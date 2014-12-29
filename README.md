A script to provision an arbitrary number of identical VMs in Azure, wrapped
in a single Hosted Service and a single Deployment.

Installation
============

Install the dependencies in `requirements.txt`.


Configuration
=============

This Azure code sample uses three configuration files:


Main Configuration
------------------

This one is passed to `--config`, and is is expected to look like this:

    {
        "service_management": {
            "subscription_id": "< your Azure Subscription ID>",
            "certificate_path": "< path to a certificate for access to the Azure API>"
        },

        "storage": {
            "account": "< the name of an Azure Storage Account you'd like to use here >",
            "access_key": "< an access key associated with the Storage Account >"
        },

        "containers": {
            "vhds": "< the name of a Azure Storage container to store VHDs in (e.g. 'vhds') >",
            "images": "< the name of an Azure Storage container store Images in (e.g. 'images') >"
        },

        "service_name": "< name to give to the Hosted Service to wrap the instances in >",
        "service_location": "< Azure region name to deploy the Hosted Service in >",
        "deployment_name": "< name to give to the Deployment to wrap the instances in >",
        "network_name": "< name of an existing Virtual Network to place the Deployment in >"

        "n_vms": < number of VMs to deploy (don't quote it!) >
    }


VM Configuration
----------------

This one is passed to `--provision`, which is expected to look like the following.
All fields are required. If you don't want something that is optional, then
provide an empty list.

    {
        "net": {
            "nat_ports": [
                {
                    "name": "ssh",
                    "protocol": "tcp",
                    "port": 22,
                    "lb": false
                },
                {
                  "name": "http",
                  "protocol": "tcp",
                  "port": 80,
                  "lb": true
                }
            ],
            "subnet_names": ["Subnet-1"],
            "public_ip_name_tpls": ["ip-{vm_name}"]
        },
        "root_disk": {
            "source_image": "b39f27a8b8c64d52b05eac6a62ebad85__Ubuntu-14_04_1-LTS-amd64-server-20141125-en-us-30GB",
            "name_tpl": "os-disk-{vm_name}",
        },
        "data_disks": [
            {
                "url_tpl": "https://testubuntug7v8mk8o.blob.core.windows.net/vhds/data-a-{vm_name}.vhd",
                "size_gb": 5
            },
            {
                "url_tpl": "https://testubuntug7v8mk8o.blob.core.windows.net/vhds/data-b-{vm_name}.vhd",
                "size_gb": 10
            }
        ],
        "system": {
            "host_name_tpl": "{vm_name}",
            "user_data_tpl": "#cloud-config\nssh_import_id: [torozco]\npackages:\n  - apache2"
        },
        "size": "Small"
    }

For fields that end in `_tpl[s]`, you can use `vm_name` to access the VM's auto-generated name.


Snapshot Configuration
----------------------

This one is passed to `--snapshot`. It is expected to look like the following:


    {
      "label_tpl": "Ubuntu 14.04 from {role.role_name}",
      "name_tpl": "ubuntu-1404-from-{role.role_name}",
      "os": "Linux"
    }

For fields that end in `_tpl[s]`, you can use `role` to access the VM that is being snapshotted.


Usage
=====

To provision, test, and then teardown your cluster, use:

    python main.py --config config.json --provision vm_config.json --test-ssh --teardown


