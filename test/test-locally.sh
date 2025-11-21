#!/bin/bash


# Identify information about the test environment
set -o allexport
. ./mongo-rs.env

OLDER_VERSION=$(echo -e "${MONGO_VERSION}\n6.0" | sort -V | head -n1)
if [[ "${OLDER_VERSION}" == "6.0" ]]; then
  MONGO_SHELL="mongosh"
else
  MONGO_SHELL="mongo"
fi
if [[ -n "$(which miniswarm)" ]] && [[ -n "$(which docker-machine)" ]]; then
  echo "The miniswarm and docker-machine commands are both available"
  DO_MINISWARM="yes"
  MANAGER_NODE="ms-manager0"
  WORKER_NODE="ms-worker0"
  SWARM_SIZE=3
else
  echo "The miniswarm and docker-machine commands are NOT both available. Using the local swarm."
  DO_MINISWARM=""
  MANAGER_NODE="${MANAGER_NODE:-}"
  WORKER_NODE="${WORKER_NODE:-}"
  SWARM_SIZE=$(docker node ls --format '{{ .Hostname }}' | xargs | wc -w)
fi
if [ ${SWARM_SIZE} -gt 0 ]; then
  echo "Tests are using a swarm size of ${SWARM_SIZE}"
  LESS_SIZE=$((SWARM_SIZE - 1))
else
  >&2 echo "Swarm Size must be at least 1 to run these tests!"
  exit 1
fi

# Define some useful functions
launchMongoClient(){
  docker run --name client --network=backend -d "mongo:${MONGO_VERSION}" tail -f /dev/null
  echo "Created MongoDB Client"
  if [[ "${MONGO_SHELL}" == "mongosh" ]]; then
    docker exec client "${MONGO_SHELL}" --nodb --eval "disableTelemetry()"
  fi
}

controller_service_mode(){
  docker service ls -f name=${CONTROLLER_SERVICE_NAME} --format "{{.Name}}:{{.Mode}}"
}

controller_service_replicas(){
  docker service ls -f name=${CONTROLLER_SERVICE_NAME} --format "{{.Name}}:{{.Replicas}}"
}

mongo_service_mode(){
  docker service ls -f name=${MONGO_SERVICE_NAME} --format "{{.Name}}:{{.Mode}}"
}

mongo_service_replicas(){
  docker service ls -f name=${MONGO_SERVICE_NAME} --format "{{.Name}}:{{.Replicas}}"
}

mongo_replicaset_size(){
  docker exec client "${MONGO_SHELL}" --quiet ${MONGO_SERVICE_NAME}/admin --eval "db.runCommand( { replSetGetStatus : 1 } )['members'].length"
}

mongo_replicaset_status(){
  docker exec client "${MONGO_SHELL}" --quiet ${MONGO_SERVICE_NAME}/admin --eval "db.runCommand( { replSetGetStatus : 1 } )['ok']"
}

# shunit 2 test functions

oneTimeSetUp(){
  if [ -n "${DO_MINISWARM}" ]; then
    miniswarm delete
    miniswarm start ${SWARM_SIZE}
    eval $(docker-machine env "${MANAGER_NODE}")
  fi
  ./build.sh
  docker network create  --attachable --opt encrypted -d overlay backend
  launchMongoClient
}

# Start a new MongoDB cluster with persistence data and checks that the controller configure it correctly.
testInitialLaunch(){
  # Make sure the data volumes don't exist yet
  # (Note - this will only get one node of the swarm... may not be enough!)
  docker volume rm mongoconfigdb || true
  docker volume rm mongodata || true

  # Deploy the swarm services and give them time to start up
  docker stack deploy -c docker-compose.yml "${STACK_NAME}" --detach=true
  sleep ${START_PERIOD_SECONDS}

  # Confirm that the services and replicaset have the expected state
  assertEquals "${MONGO_SERVICE_NAME}:global" "$(mongo_service_mode)"
  assertEquals "${CONTROLLER_SERVICE_NAME}:replicated" "$(controller_service_mode)"
  assertEquals "${MONGO_SERVICE_NAME}:${SWARM_SIZE}/${SWARM_SIZE}" "$(mongo_service_replicas)"
  assertEquals "${CONTROLLER_SERVICE_NAME}:1/1" "$(controller_service_replicas)"
  assertEquals "1" "$(mongo_replicaset_status)"
  assertEquals "${SWARM_SIZE}" "$(mongo_replicaset_size)"
}

# Kill one mongo container
testKillMongo(){
  # Kill one mongo container and confirm it's gone
  container=$(docker ps -f name=${MONGO_SERVICE_NAME} --format {{.ID}})
  docker kill ${container}
  sleep ${SHUTDOWN_PERIOD_SECONDS}
  assertEquals "${MONGO_SERVICE_NAME}:${LESS_SIZE}/${SWARM_SIZE}" "$(mongo_service_replicas)"

  sleep ${START_PERIOD_SECONDS}  # time needed for new mongo container to respawn, replacing the killed one.
  assertEquals "${MONGO_SERVICE_NAME}:${SWARM_SIZE}/${SWARM_SIZE}" "$(mongo_service_replicas)"
  assertEquals "1" "$(mongo_replicaset_status)"
  assertEquals "${SWARM_SIZE}" "$(mongo_replicaset_size)"
}

# Drain a Swarm Worker Node (where one of the mongo service replicas is deployed)
testDrainWorker(){
  if [[ -z "${WORKER_NODE}" ]]; then startSkipping; assertEquals 1 1; return; fi

  # Drain a swarm worker node.
  docker node update "${WORKER_NODE}" --availability drain
  docker node update "${WORKER_NODE}" --availability pause
  sleep ${SHUTDOWN_PERIOD_SECONDS}  # allow container on drained worker node to exit
  assertEquals "${MONGO_SERVICE_NAME}:${LESS_SIZE}/${LESS_SIZE}" "$(mongo_service_replicas)"

  # Give the mongo service time to respawn a replica if it was going to
  sleep ${START_PERIOD_SECONDS}

  # Confirm that the replicaset has reconfigured OK with one fewer members than before
  assertEquals "${MONGO_SERVICE_NAME}:${LESS_SIZE}/${LESS_SIZE}" "$(mongo_service_replicas)"
  assertEquals "1" "$(mongo_replicaset_status)"
  assertEquals "${LESS_SIZE}" "$(mongo_replicaset_size)"
}

# Re-Start Swarm-2 worker node
testReactivateWorker(){
  if [[ -z "${WORKER_NODE}" ]]; then startSkipping; assertEquals 1 1; return; fi

  # Activate the previously-drained worker node
  docker node update "${WORKER_NODE}" --availability active
  sleep ${START_PERIOD_SECONDS}

  # Confirm that the replicaset has been reconfigured with the full quota of members
  assertEquals "${MONGO_SERVICE_NAME}:${SWARM_SIZE}/${SWARM_SIZE}" "$(mongo_service_replicas)"
  assertEquals "1" "$(mongo_replicaset_status)"
  assertEquals "${SWARM_SIZE}" "$(mongo_replicaset_size)"
}

# Drain the Swarm-Manager node which has both the controller and a mongo replica on it
testDrainManager(){
  if [[ -z "${MANAGER_NODE}" ]] || [[ -z "${WORKER_NODE}" ]]; then startSkipping; assertEquals 1 1; return; fi

  # First, promote one a worker node to manager,
  # and reconnect the docker client to that manager
  docker node promote "${WORKER_NODE}"
  docker rm -fv client
  eval $(docker-machine env "${WORKER_NODE}")
  launchMongoClient

  # THen drain the old manger node
  docker node update "${MANAGER_NODE}" --availability drain
  docker node update "${MANAGER_NODE}" --availability pause

  # Wait long enough to restart both containers AND sort out the replicaset
  sleep $(($START_PERIOD_SECONDS + $CONTROL_INTERVAL_SECONDS))

  # Confirm that all services are back, but the replicaset now has fewer members
  assertEquals "${MONGO_SERVICE_NAME}:${LESS_SIZE}/${LESS_SIZE}" "$(mongo_service_replicas)"
  assertEquals "${CONTROLLER_SERVICE_NAME}:1/1" "$(controller_service_replicas)"
  assertEquals "1" "$(mongo_replicaset_status)"
  assertEquals "${LESS_SIZE}" "$(mongo_replicaset_size)"
}

testReactivateManager(){
  if [[ -z "${MANAGER_NODE}" ]] || [[ -z "${WORKER_NODE}" ]]; then startSkipping; assertEquals 1 1; return; fi

  # Re-activate the previously-drained manager node
  # and re-launch our mongo_client on it
  docker node update "${MANAGER_NODE}" --availability active
  docker rm -fv client
  eval $(docker-machine env "${MANAGER_NODE}")
  launchMongoClient

  # Demote the previously-promoted worker
  docker node demote "${WORKER_NODE}"

  # Wait long enough to restart both containers AND sort out the replicaset
  sleep $(($START_PERIOD_SECONDS + $CONTROL_INTERVAL_SECONDS))

  # Confirm that the replicaset is back to full size and status
  assertEquals "${MONGO_SERVICE_NAME}:${SWARM_SIZE}/${SWARM_SIZE}" "$(mongo_service_replicas)"
  assertEquals "${CONTROLLER_SERVICE_NAME}:1/1" "$(controller_service_replicas)"
  assertEquals "1" "$(mongo_replicaset_status)"
  assertEquals "${MONGO_SERVICE_NAME}:${SWARM_SIZE}/${SWARM_SIZE}" "$(mongo_service_replicas)"
}


# Remove and re-launch the whole swarm stack
testRelaunchSwarmStack(){
  docker stack rm mongo
  sleep ${SHUTDOWN_PERIOD_SECONDS}

  # Redeploy the swarm services and give them time to start up
  docker stack deploy -c docker-compose.yml "${STACK_NAME}" --detach=true
  sleep ${START_PERIOD_SECONDS}

  assertEquals "${MONGO_SERVICE_NAME}:global" "$(mongo_service_mode)"
  assertEquals "${CONTROLLER_SERVICE_NAME}:replicated" "$(controller_service_mode)"
  assertEquals "${MONGO_SERVICE_NAME}:${SWARM_SIZE}/${SWARM_SIZE}" "$(mongo_service_replicas)"
  assertEquals "${CONTROLLER_SERVICE_NAME}:1/1" "$(controller_service_replicas)"
  assertEquals "1" "$(mongo_replicaset_status)"
  assertEquals "${SWARM_SIZE}" "$(mongo_replicaset_size)"
}

# Force an update of just the MongoDB service
testUpdateMongoService(){
  docker service update --force ${MONGO_SERVICE_NAME}
  sleep ${START_PERIOD_SECONDS}

  assertEquals "${MONGO_SERVICE_NAME}:global" "$(mongo_service_mode)"
  assertEquals "${MONGO_SERVICE_NAME}:${SWARM_SIZE}/${SWARM_SIZE}" "$(mongo_service_replicas)"
  assertEquals "1" "$(mongo_replicaset_status)"
  assertEquals "${SWARM_SIZE}" "$(mongo_replicaset_size)"
}


oneTimeTearDown(){
  docker rm -fv client
  docker stack remove ${STACK_NAME}
  sleep ${SHUTDOWN_PERIOD_SECONDS}
  docker network rm ${BACKEND_NETWORK_NAME}
  if [ -n "${DO_MINISWARM}" ]; then
    miniswarm delete
  fi
}

# load shunit2
. shunit2
