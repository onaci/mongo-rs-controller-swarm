#!/bin/sh

MONGO_HOST=$(hostname --ip-address || echo '127.0.0.1')

testScaleUpMongo(){
   docker service scale mongo_mongo=4
   sleep 100
   result=$(docker service ls -f name=mongo_mongo --format "{{.Name}}:{{.Replicas}}")
   assertEquals "mongo_mongo:4/4" "${result}"
}

testMongoClusterStatusAfterScaleUp(){
   sleep 10
   result=$("${MONGO_SHELL:-mongosh}" --quiet "${MONGO_HOST}/admin" --eval "db.runCommand( { replSetGetStatus : 1 } )['ok']")
   assertEquals "1" "${result}"
}

testMongoClusterSizeAfterScaleUp(){
   sleep 10
   result=$("${MONGO_SHELL:-mongosh}" --quiet "${MONGO_HOST}/admin" --eval "db.runCommand( { replSetGetStatus : 1 } )['members'].length")
   assertEquals "4" "${result}"
}

testScaleDownMongo(){
   docker service scale mongo_mongo=3
   sleep 100
   result=$(docker service ls -f name=mongo_mongo --format "{{.Name}}:{{.Replicas}}")
   assertEquals "mongo_mongo:3/3" "${result}"
}


testMongoClusterStatusAfterScaleDown(){
   sleep 10
   result=$("${MONGO_SHELL:-mongosh}" --quiet "${MONGO_HOST}/admin" --eval "db.runCommand( { replSetGetStatus : 1 } )['ok']")
   assertEquals "1" "${result}"
}

testMongoClusterSizeAfterScaleDown(){
   sleep 10
   result=$("${MONGO_SHELL:-mongosh}" --quiet "${MONGO_HOST}/admin" --eval "db.runCommand( { replSetGetStatus : 1 } )['members'].length")
   assertEquals "3" "${result}"
}

# load shunit2
. shunit2
