"""
This script is used to setup and maintain a MongoDB replicaset on a Docker Swarm.

IMPORTANT: It is intended to be used with the docker-compose.yml in the mongodb replica recipe.
This basically means 2 things:
 1 - There should not be more than one process running this script in the swarm.
 2 - There should be maximum one replica per swarm node. This is achieved using "global" deployment mode.

HOW IT WORKS (Overview):
- Scans running mongod instances in the swarm
- Checks if a replicaset is already configured
- If configured:
    - Loads the replicaset configuration
    - If the old replicaset lost the primary node, waits for a new election. In lack of a new primary,
    it forces a reconfiguration.
- If not configured:
    - Picks an arbitrary instance to act as a replicaset primary
    - Configures the replicaset on it
- Keeps on listening to changes in the original set of mongod instances IPs
- Reconfigures replicaset if a change was perceived.

INPUT: Via environment variables. See get_required_env_variables.

# TODO: Add tests
"""
from pymongo.errors import OperationFailure, ServerSelectionTimeoutError
import docker
import logging
import math
import os
import pymongo as pm
import signal
import sys
import time


# region Configuration Settings

def get_required_env_variables():
    REQUIRED_VARS = [
        'OVERLAY_NETWORK_NAME',
        'MONGO_SERVICE_NAME',
        'REPLICASET_NAME',
    ]
    envs = {}
    for rv in REQUIRED_VARS:
        envs[rv.lower()] = os.environ[rv]

    if not all(envs.values()):
        raise RuntimeError("Missing required ENV variables. {}".format(envs))

    OPTIONAL_INT_VARS = [
        'MONGO_PORT',
        'START_INTERVAL_SECONDS',
        'START_PERIOD_SECONDS',
        'CONTROL_INTERVAL_SECONDS'
    ]
    for ov in OPTIONAL_INT_VARS:
        if ov in os.environ:
            envs[ov.lower()] = int(os.environ[ov])
    return envs


# endregion


# region Docker Swarm Tasks
def get_service_task_ips(
    dc: docker.Client,
    service_name: str,
    overlay_network_name: str,
) -> set(str):
    """Identify the IP addresses for running swarm-service tasks on a specified overlay network.

    :param dc:
        A docker API Client connection.
    :param service_name:
        Name of the Docker Swarm Service which tasks should be associated with
    :param overlay_network_name:
        Name of the Docker Swarm overlay network which you want IP addresses on.
    :return:
        A set of unique IP addresses for any running tasks matching these criteria
        (Will be empty if no tasks are currently running)
    """
    task_ips = set()
    logger = logging.getLogger(__name__)
    try:
        services = dc.services.list(filters={'name': service_name})
        if len(services) == 0:
            logger.warning(f"Could not find docker swarm service with name '{service_name}'")
        elif len(services) > 1:
            logger.error(f"There are multiple docker swarm services named '{service_name}': {services}")
        else:
            candidate_tasks = services[0].tasks(filters={'desired-state': "running"})
            if len(candidate_tasks) == 0:
                logger.warning(f"Docker swarm service '{service_name}' exists but has no candidate tasks")
            else:
                running_tasks = [
                    t for t in candidate_tasks
                    if t['Status']['State'] == "running"
                ]
                logger.debug(f"Docker swarm service '{service_name}' exists and has {len(running_tasks)} running of {len(candidate_tasks)} candidate tasks: {candidate_tasks}")
                for t in running_tasks:
                    for n in t['NetworksAttachments']:
                        if n['Network']['Spec']['Name'] == overlay_network_name:
                            ip = n['Addresses'][0].split('/')[0]  # clean prefix from ip
                    task_ips.add(ip)
    except Exception:
        logger.exception(f"Unexpected exception retrieving running tasks for service '{service_name}'")
    return task_ips


def wait_for_service_task_ips(
    dc: docker.Client,
    service_name: str,
    overlay_network_name: str,
    start_interval_seconds: int = 5,
    start_period_seconds: int = 60
) -> set(str):
    """Poll for Docker Swarm Service Tasks until they are ready.

    :param dc:
        A docker API Client connection.
    :param service_name:
        Name of the Docker Swarm Service which tasks should be associated with
    :param overlay_network_name:
        Name of the Docker Swarm overlay network which you want IP addresses on.
    :param start_interval_seconds:
        The duration (in seconds) to wait between attempts to discover whether a (re)starting
        `mongo_service_name` service is ready to accept connections yet.
        (Like the `start_interval` for docker service healthchecks)
    :param start_period_seconds:
        The total duration (in seconds) of grace that a (re)starting `mongo_service_name` service
        is expected to take to start up and be ready to accept connections.
        (Like the `start_period` setting for a docker service healthcheck)
    :return:
        A set of unique IP addresses for any running tasks matching these criteria
        (Will be empty if no tasks are currently running)

    """
    logger = logging.getLogger(__name__)
    wait_count_max = math.ceil(start_period_seconds / start_interval_seconds)
    wait_count = 0
    while wait_count <= wait_count_max:
        task_ips = get_service_task_ips(
            dc=dc,
            service_name=service_name,
            overlay_network_name=overlay_network_name
        )
        if task_ips:
            logger.log(
                level=(logging.INFO if (wait_count > 0) else logging.DEBUG),
                msg=f"Docker service '{service_name}' is up with {len(task_ips)} running tasks attached to the '{overlay_network_name}' network."
            )
            return task_ips
        else:
            logger.info(msg=f"Docker swarm service '{service_name}' does not have running tasks yet. Retry in {start_interval_seconds}s ({wait_count} of {wait_count_max})")
            time.sleep(start_interval_seconds)
            wait_count += 1

    logger.warning(f"Timed out waiting for Docker swarm service '{service_name}' to have running tasks attached to the '{overlay_network_name}' network")
    return set()

# endregion


# region MongoDB Replicasets

def init_replicaset(
    member_hosts: set(str),
    replicaset_name: str
) -> None:
    """
    Init a MongoDB replicaset from scratch.

    :param member_hosts:
        The host-addresses which MongoDB databases that *should* be in the replicaset are listening on.
    :param replicaset_name:
        The name which should identify the replicaset on these hosts.
    :return:
        The host-address which should be the new replicaset's initial primary.
    """
    assert len(member_hosts) > 0
    logger = logging.getLogger(__name__)

    connect_host = list(member_hosts)[0]
    rs_config = {
        '_id': replicaset_name,
        'members': [
            {'_id': i, 'host': member_host}
            for i, member_host in enumerate(member_hosts)
        ],
        'version': 1
    }
    logger.debug(f"Initial replicaset configuration: {rs_config}, connect_host: {connect_host}")
    with pm.MongoClient(host=connect_host, directConnection=True) as primary:
        try:
            res = primary.admin.command("replSetInitiate", rs_config)
        except OperationFailure as e:
            logger.debug(f"replSetInitiate already configured, forcing configuration ({e})")
            res = primary.admin.command("replSetReconfig", rs_config, force=True)
        logger.info(f"replSetInitiate: {res}")
    return connect_host


def get_replicaset_hosts(
    expected_hosts: set(str),
    replicaset_name: str,
) -> tuple[set, str | None]:
    """Retrieve the host-addresses for all *current* replicaset members.

    :param expected_hosts:
        The host-addresses for member hosts that *should* be in the replicaset.
    :param replicaset_name:
        The name of the replicaset that we are wanting hosts for.
    :return:
        A tuple consisting of:
        - A set of host-addresses for all known replicaset members
        - The host-address of the current replicaset primary (or None if there is no current primary)
    """
    rs_members = set()
    rs_primary = None
    logger = logging.getLogger(__name__)
    for host in expected_hosts:
        try:
            with pm.MongoClient(host=host, directConnection=True) as mc:
                rs_config = mc.admin.command("replSetGetConfig")['config']
                rs_id = rs_config.get('_id')
                if rs_id == replicaset_name:
                    logger.debug(f"Host '{host} is a current member of replicaset '{replicaset_name}'. is_primary = {mc.is_primary}")
                    rs_members.update([
                        m['host'] for m in rs_config['members']
                    ])
                    if mc.is_primary:
                        rs_primary = host
                else:
                    logger.warning(f"Host '{host} is a current member of an unexpected replicaset '{rs_id}'")
        except ServerSelectionTimeoutError as sste:
            logger.warning(f"Host '{host}' timed out replicaset configuration check: {sste}")
        except OperationFailure as of:
            logger.debug(f"Host '{host}' has no current replicaset configuration: {of}")
        except Exception:
            logger.exception(level=logging.WARNING, msg=f"Unexpected error checking replicaset configuration for host '{host}'")

    logger.debug(f"Current replicaset configuration: members = {rs_members}, primary = {rs_primary}")
    return rs_members, rs_primary


def update_replicaset(
    connect_host: str,
    remove_hosts: set(str),
    add_hosts: set(str)
) -> None:
    """Update the MongoDB Replicaset to ensure it has the correct set of members.

    Actually not too different from what mongo does:
    https://github.com/mongodb/mongo/blob/master/src/mongo/shell/utils.js

    Note: MongoDB can only add or remove one voting member at a time!
    https://www.mongodb.com/docs/manual/reference/command/replSetReconfig/#std-label-replSetReconfig-cmd-single-node

    :param connect_host:
        The MongoDB host to connect to in order to make this change.
        Ideally the current primary, or at least a current member of the replicaset
        (will be the new primary after this reconfiguration)
    :param remove_hosts:
        The MongoDB host-addresses to remove as current replicaset members.
        Must not include `connect_host`.
    :param add_hosts:
        The MongoDB host-addresses to add as new replicaset members.
        May include `connect_host` if all other current members are being removed.
    """
    logger = logging.getLogger(__name__)
    try:
        assert remove_hosts or add_hosts
        assert connect_host not in remove_hosts
        with pm.MongoClient(host=connect_host, directConnection=True) as cli:
            # Retrieve the *current* replicaset configuration
            rs_status = cli.admin.command("replSetGetStatus").get('ok', 0)
            rs_config = cli.admin.command("replSetGetConfig")['config']
            logger.debug(f"Old Configs: {rs_config}")
            rs_members = rs_config['members']

            # Impose the new replicaset configuration,
            # forcing the change if that is what it takes.
            if remove_hosts:
                logger.info(f"To remove: {remove_hosts}")
                rs_config['members'] = [m for m in rs_members if m['host'] not in remove_hosts]

            if add_hosts:
                logger.info(f"To add: {add_hosts}")
                if rs_members:
                    next_id = max([m['_id'] for m in rs_members]) + 1
                else:
                    next_id = 0
                for add_host in add_hosts:
                    rs_config['members'].append({
                        '_id': next_id,
                        'host': add_host
                    })
                    next_id += 1

            rs_config['version'] += 1
            force_required = (
                not cli.is_primary
                or (rs_status != 1)
                or ((len(remove_hosts) + len(add_hosts)) > 1)
            )
            logger.debug(f"New config: {rs_config}, force required to impose it? {force_required}")
            res = cli.admin.command("replSetReconfig", rs_config, force=force_required)
            logger.info(f"replSetReconfig: {res}")
    except Exception:
        logger.exception("Unexpected exception updating replicaset configuration")


def ensure_replicaset(
    expected_hosts: set(str),
    replicaset_name: str
) -> None:
    """
    Ensure that the MongoDB replicaset is configured with the expected set of members.

    If there was no replica before, create one from scratch.
    If there was already replica (e.g, this script was restarted), force a reconfiguration if
    the membership list doesn't match the one we want.

    :param member_hosts:
        The host-addresses which MongoDB databases that *should* be in the replicaset are listening on.
    :param replicaset_name:
        The name which should identify the replicaset on these hosts.
    """
    logger = logging.getLogger(__name__)

    current_hosts, current_primary = get_replicaset_hosts(
        expected_hosts=expected_hosts,
        replicaset_name=replicaset_name
    )

    if current_hosts.symmetric_difference(expected_hosts):
        logger.info(f"Membership change detected: {current_hosts} -> {expected_hosts}")
        to_keep = current_hosts.intersection(expected_hosts)
        if to_keep:
            logger.info(f"{len(to_keep)} members of the previous replicaset '{replicaset_name}' are being retained")
            update_replicaset(
                connect_host=(current_primary if current_primary in to_keep else list(to_keep)[0]),
                remove_hosts=(current_hosts - to_keep),
                add_hosts=(expected_hosts - current_hosts)
            )
        else:
            logger.info(f"No previous valid configuration, starting replicaset '{replicaset_name}' from scratch")
            init_replicaset(
                member_hosts=expected_hosts,
                replicaset_name=replicaset_name
            )
    else:
        logger.info(f"Primary is: {current_primary}")

# endregion


# region Main

_EXIT_REQUESTED = False


def request_exit(signum, frame):
    global _EXIT_REQUESTED
    _EXIT_REQUESTED = True
    logging.getLogger(__name__).warning("Exit request received")


def manage_replica(
    dc: docker.Client,
    mongo_service_name: str,
    overlay_network_name: str,
    replicaset_name: str,
    mongo_port: int = 27017,
    start_interval_seconds: int = 5,
    start_period_seconds: int = 60,
    control_interval_seconds: int = 10,
) -> bool:
    """MongoDB Replicaset Controller.

    To manage the replica is to:
    - Configure replicaset
        If there was no replica before, create one from scratch.
        If there was a replica (e.g, this script was restarted), that replicaset could be either fine or broken.
            If the replicaset was healthy, move on to the "watching" phase.
            Else, force a reconfiguration.
    - Watch for changes in tasks ips
        When IP changes are detected, the replica will break, so we must fix it on the fly.

    :param dc:
        A docker API Client connection to the docker swarm that the mongo service is running on.
    :param mongo_service_name:
        Name of the Docker Swarm Service made up of MongoDB container tasks
    :param overlay_network_name:
        Name of the Docker Swarm overlay network which the MongoDB service tasks should be communicating
        with each other on to keep the replicaset in synch.
    :param replicaset_name:
        Identifier for the replicaset that the MongoDB service tasks should be members of.
    :param mongo_port:
        Port number that the MongoDB service tasks should be listening for client-connections
        from this controller *and* from each other on.
    :param start_interval_seconds:
        The number of seconds to wait between attempts to discover whether a (re)starting
        `mongo_service_name` service is ready to accept connections yet.
        (Like the `start_interval` for docker service healthchecks)
    :param start_period_seconds:
        The total number of seconds to wait for the `mongo_service_name` service tasks to be ready
        to accept connections before assuming that it has failed to start up.
        (Like the `start_period` setting for a docker service healthcheck)
    :param control_interval_seconds`:
        The number of seconds to wait between attempts to ensure that the MongoDB
        replicaset is configured and functioning correctly.

    :return:
        `True` if the management loop has exited gracefully.
        `False` if the management loop has exited due to an error condition.
    """
    logger = logging.getLogger(__name__)
    while True:
        # Act on any exit request
        if _EXIT_REQUESTED:
            logger.info("Exiting as requested")
            return True

        # Identify the docker swarm service tasks which are our Mongo replicaset members,
        # and bail if there are still none listening after the configured startup period.
        mongo_task_ips = wait_for_service_task_ips(
            dc=dc,
            service_name=mongo_service_name,
            overlay_network_name=overlay_network_name,
            start_interval_seconds=start_interval_seconds,
            start_period_seconds=start_period_seconds
        )
        if not mongo_task_ips:
            logger.error(f"Unable to identify MongoDB host addresses for tasks of the '{mongo_service_name}' service.")
            return False

        # Ensure the replicaset is made up of  these docker swarm-service members
        ensure_replicaset(
            expected_hosts=set([f'{ip}:{mongo_port}' for ip in mongo_task_ips]),
            replicaset_name=replicaset_name
        )

        # Wait a bit before checking again
        time.sleep(control_interval_seconds)


if __name__ == '__main__':
    # Initialise simple logging to stderr
    logging.basicConfig(
        level=(logging.DEBUG if 'DEBUG' in os.environ else logging.INFO),
        format='%(asctime)s.%(msecs)03d %(levelname)s [%(name)s:%(lineno)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    try:
        # Keep an eye out for exit signals...
        signal.signal(signal.SIGINT, request_exit)
        signal.signal(signal.SIGTERM, request_exit)

        # Use the local environment's docker engine and configuration settings
        dc = docker.from_env()
        envs = get_required_env_variables()

        # Manage the replicaset until exit or error,
        graceful_exit = manage_replica(dc=dc, **envs)
    except Exception:
        logging.getLogger(__name__).exception('Unexpected exception managing replicaset')
        graceful_exit = False

    sys.exit(0 if graceful_exit else 1)
