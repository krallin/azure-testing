#coding:utf-8
import sys
import os
import argparse
import json
import socket
import logging
import random
import base64
import uuid
import time

from azure import *
from azure.servicemanagement import *

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


# TODO - Actually pass those as parameters!
VM_SIZE = "Small"

# Images are a resource that span services
IMAGE_NAME = "b39f27a8b8c64d52b05eac6a62ebad85__Ubuntu-14_04_1-LTS-amd64-server-20141125-en-us-30GB"

# Networks are a resource that span services
NET_NAME = "Group Group thomas-tests"
NET_SUBNET = "Subnet-1"

# If using an Image
# Not a good idea, because there are no VM images for Ubuntu!!! (only a few SQL Server and HortonWorks boxes)
# Containers are a resource that span services
BLOB_CONTAINER = "https://testubuntug7v8mk8o.blob.core.windows.net/vhds/"

CLOUD_INIT_CUSTOM_DATA = """#cloud-config
ssh_import_id: [torozco]
"""


def ssh_up(name, host, port, timeout=5):
    try:
        s = socket.create_connection((host, port), timeout)
        s.settimeout(timeout)
        data = s.recv(64).strip()
    except (socket.error, socket.timeout) as e:
        logger.debug("SSH connection failed with: %s", e)
        return False

    if not data.startswith('SSH'):
        logger.warning("SSH did not respond with 'SSH' header: '%s'", data)
        return False

    logger.debug("SSH responded with 'SSH' header: '%s'", data)
    return True


class OperationFailed(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message

    def __repr__(self):
        return "{0} ({1})".format(self.code, self.message)


def wait_for_operation(operation):
    while 1:
        status = sms.get_operation_status(operation.request_id)
        logger.debug("Operation status is '%s'", status.status)
        if status.status != "InProgress":
            break
        time.sleep(1)

    if status.error is not None:
        raise Operation(status.error.code, status.error.message)

    logger.info("Operation '%s' completed with '%s'", operation.request_id, status.status)


def _get_os_disk_configuration_for_vm(vm_name):
    disk_name = "os-disk-{0}".format(vm_name)
    disk_url = "{0}/{1}.vhd".format(BLOB_CONTAINER, vm_name)  # /!\ This must be found under /vhds/ !! Can't use the root

    return OSVirtualHardDisk(
        source_image_name=IMAGE_NAME,
        disk_name=disk_name,
        media_link=disk_url
    )


def create_hosted_service(sms, service_name, service_location):
    # Does the service exist?
    try:
        service = sms.get_hosted_service_properties(service_name)
    except WindowsAzureMissingResourceError:
        logger.info("Service '%s' does not exist, creating", service_name)
        sms.create_hosted_service(service_name=service_name, label=service_name, location=service_location)
    else:
        real_location = service.hosted_service_properties.location
        if real_location != service_location:
            logger.warning("Service '%s' exists, but its Location is '%s', not: %s'.", service_name, real_location, service_location)


def deploy_vm(sms, service_name, deployment_name, vm_name, nat_ssh_port):
    # http://msdn.microsoft.com/en-us/library/azure/jj157194.aspx#bk_rolelist
    network_config = ConfigurationSet()
    network_config.input_endpoints.input_endpoints.append(ConfigurationSetInputEndpoint(
        name = "ssh",
        protocol = "tcp",
        local_port = 22,
        port = nat_ssh_port,
    ))
    network_config.subnet_names.append(NET_SUBNET)  # TODO
    # Note that for some reason, not giving the IPs a name results in some form of conflict (which does *not* throw an error),
    # where only one instance will get a Public IP. Giving the IPs a name seems to resolve the issue (note that it doesn't seem to
    # matter whether that name is unique or not, but if we're giving the IP a name, we might as well make it unique)
    network_config.public_ips.public_ips.append(PublicIP(name="ip-{0}".format(vm_name)))

    kwargs = {
        "service_name" : service_name,
        "deployment_name" : deployment_name,

        # Actual VM params
        "role_name" : vm_name,

        "os_virtual_hard_disk" : _get_os_disk_configuration_for_vm(vm_name),

        "system_config" : LinuxConfigurationSet(
            # Plenty of stuff we don't need!
            host_name = vm_name,
            user_name = "thomas",
            user_password = base64.b64encode(open("/dev/urandom").read(32)),
            disable_ssh_password_authentication = True,

            # Finally, something we need!
            custom_data = CLOUD_INIT_CUSTOM_DATA,
        ),
        "network_config" : network_config,

        "role_size" : VM_SIZE,
    }

    try:
        deployment = sms.get_deployment_by_name(service_name, deployment_name)
    except WindowsAzureMissingResourceError:
        logger.info("Deployment '%s' does not exist in Service '%s'", deployment_name, service_name)
        method = sms.create_virtual_machine_deployment
        kwargs.update({
            "deployment_slot" : "Production",
            "label" : vm_name,
            "virtual_network_name" : NET_NAME,
        })
    else:
        method = sms.add_role

    logger.info("Creating VM: %s", vm_name)

    operation = method(**kwargs)

    try:
        wait_for_operation(operation)
    except OperationFailed as e:
        logger.error("An error occured: '%s'", e.message)
    else:
        logger.info("VM '%s' is ready, ssh to '%s.cloudapp.net -p %s'", vm_name, service_name, nat_ssh_port)


def ping_vms(sms, service_name, deployment_name):
    logger.info("Retrieving list of VMs and ports")
    deployment = sms.get_deployment_by_name(service_name, deployment_name)
    ping_targets = []

    for vm in deployment.role_instance_list:
        for endpoint in vm.instance_endpoints:
            # Note: the endpoint ports are strings. Not that this makes sense, but we have to convert
            # those to ints.
            if int(endpoint.local_port) == 22:
                ping_targets.append((vm.instance_name, endpoint.vip, int(endpoint.public_port)))
        if not vm.public_ips:
            logger.warning("VM %s has no public IPs", vm.instance_name)
        else:
            for public_ip in vm.public_ips:
                ping_targets.append((vm.instance_name, public_ip.address, 22))

    logger.info("Ping Targets: %s", ping_targets)
    while ping_targets:
        ping_target = ping_targets.pop(0)
        logger.debug("Trying to hit SSH on %s at %s:%s", *ping_target)
        if ssh_up(*ping_target):
            logger.info("SSH is up on %s at %s:%s", *ping_target)
        else:
            logger.info("SSH is not up on %s at %s:%s", *ping_target)
            ping_targets.append(ping_target)
            time.sleep(1)


def teardown_hosted_service(sms, service_name):
    try:
        service = sms.get_hosted_service_properties(service_name, embed_detail = True)
    except WindowsAzureMissingResourceError:
        logger.info("Service '%s' was already deleted", service_name)
        return

    for deployment in service.deployments:
        disks_to_delete = []

        # Deleting the Deployment tends to bug out and leave a few disks un-deletable for
        # while because they are still attached. We instead delete the VMs, then just poll
        # the disks until we can delete them.
        for role in deployment.role_list:
            # Note that we cannot delete individual VMs here: the last VM cannot be deleted
            # unelss we also delete the deployment.
            disks_to_delete.append(role.os_virtual_hard_disk)

        logger.info("Deleting Deployment '%s'", deployment.name)
        op = sms.delete_deployment(service_name, deployment.name)
        wait_for_operation(op)

        while disks_to_delete:
            candidate = disks_to_delete.pop(0)

            if not hasattr(candidate, 'name'):
                # Disk has a name, but OSVirtualHardDisk has disk_name
                candidate.name = candidate.disk_name

            if hasattr(candidate, "attached_to"):
                # We've already seen this guy, it's a real disk. Pace ourselves.
                logger.debug("Disk '%s' was attached when we checked, waiting", candidate.name)
                time.sleep(10)

            # Check if any progress was made
            real_disk = sms.get_disk(candidate.name)
            if real_disk.attached_to is not None:
                logger.debug("Disk '%s' is still attached to '%s'", real_disk.name, real_disk.attached_to.role_name)
                disks_to_delete.append(real_disk)
                continue

            logger.info("Deleting Disk '%s'", real_disk.name)
            sms.delete_disk(real_disk.name, delete_vhd=True)


    logger.info("Deleting Service '%s'", service_name)
    sms.delete_hosted_service(service_name)


def random_vm_name():
    return str(uuid.uuid4())

def random_vm_port():
    return random.randint(2**12, 2**16) - 1


if __name__ == "__main__":
    #################
    # Configuration #
    #################

    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", required=True)
    parser.add_argument('--provision', action='store_true')
    parser.add_argument('--ping', action='store_true')
    parser.add_argument('--teardown', action='store_true')
    ns = parser.parse_args()

    with open(ns.config) as f:
        config = json.load(f)

    # Those keys MUST be in the configuration file
    config_keys = [
            "subscription_id", "certificate_path",
            "service_name", "service_location",
            "deployment_name", "n_vms"]
    for cnf_key in config_keys:
        try:
            locals()[cnf_key] = config[cnf_key]
        except KeyError:
            logger.error("Missing configuration key: %s", cnf_key)
            sys.exit(1)

    ###################
    # Actual Workflow #
    ###################

    sms = ServiceManagementService(subscription_id, certificate_path)

    if ns.provision:
        create_hosted_service(sms, service_name, service_location)

        vm_configs = [(random_vm_name(), random_vm_port()) for _ in range(n_vms)]
        for vm_config in vm_configs:
            deploy_vm(sms, service_name, deployment_name, *vm_config)

    if ns.ping:
        ping_vms(sms, service_name, deployment_name)

    if ns.teardown:
        teardown_hosted_service(sms, service_name)
