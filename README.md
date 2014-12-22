A script to provision an arbitrary number of identical VMs in Azure, wrapped
in a single Hosted Service and a single Deployment.

Installation
============

Install the dependencies in `requirements.txt`.


Configuration
=============

This Azure code sample uses two configuration files:

The one passed to `--config`, which is expected to look like this:

    {
        "subscription_id": "< your Azure Subscription ID>",
        "certificate_path": "< path to a certificate for access to the Azure API>",

        "service_name": "< name to give to the Hosted Service to wrap the instances in >",
        "service_location": "< Azure region name to deploy the Hosted Service in >",
        "deployment_name": "< name to give to the Deployment to wrap the instances in >",

        "n_vms": < number of VMs to deploy (don't quote it!) >
    }

The one passed to `--provision`, which is expected to look like the following.
All fields are required. If you don't want something that is optional, then
provide an empty list.

    {
        "net": {
            "nat_ports": [
                {
                    "name": "port-ssh",
                    "protocol": "tcp",
                    "port": 22
                }
            ],
            "network_name": "Group Group thomas-tests",
            "subnet_names": ["Subnet-1"],
            "public_ip_name_tpls": ["ip-{vm_name}"]
        },
        "root_disk": {
            "source_image": "b39f27a8b8c64d52b05eac6a62ebad85__Ubuntu-14_04_1-LTS-amd64-server-20141125-en-us-30GB",
            "name_tpl": "os-disk-{vm_name}",
            "url_tpl": "https://testubuntug7v8mk8o.blob.core.windows.net/vhds/{vm_name}.vhd"
        },
        "system": {
            "host_name_tpl": "{vm_name}",
            "user_data_tpl": "#cloud-config\nssh_import_id: [torozco]\n"
        },
        "size": "Small"
    }

For fields that end in `_tpl[s]`, you can use `vm_name` to access the VM's
auto-generated random name, and `vm_config` to address the tree you defined
in the configuration.


Usage
=====

To provision, test, and then teardown your cluster, use:

    python main.py -c config.json --provision vm_config.json --ping --teardown
