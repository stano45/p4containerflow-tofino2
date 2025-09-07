# p4containerflow-tofino2

TODO: introduction

- [p4containerflow-tofino2](#p4containerflow-tofino2)
  - [Development setup](#development-setup)
  - [Rewriting a p4 program from v1model to t2na](#rewriting-a-p4-program-from-v1model-to-t2na)
  - [Writing a control plane](#writing-a-control-plane)
  - [Troubleshooting](#troubleshooting)
    - [run\_switchd](#run_switchd)
    - [bfshell](#bfshell)


## Development setup

The Tofino 2 switch (for simplicity, we will refer to it as "the switch" from this point on) is running Ubuntu 22.04.04 LTS. This OS was already installed when we started using the switch, so we will not describe this process here. Refer to the Intel Tofino documentation for instructions.

The switch and the development machine are both connected to the same network, therefore we can simply use SSH to connect to the switch.

In order to compile and run p4 programs on the switch, we need to install the P4 Software Development Environment (SDE). This is done via P4Studio (proprietary component provided by Intel). Again, refer to the Intel website for software artifacts. We used version `9.13.4` of the SDE (the latest version as of November 2024).

After extracting the `bf-sde-x.y.z.tgz`, run P4Studio in interactive mode, and set the target to be the ASIC hardware (not ASIC model). For reference, we included the configuration file for our SDE build in [p4studio-profile.yaml](p4studio-profile.yaml).

In our installation, we had to also manually build any examples we wanted to run. This can be done by running `./p4studio build <example_name>`. Also, the required Kernel modules were not automatically loaded, so we wrote a simple [script](scripts/load_kernel_modules.sh) and added it as a startup service.

Finally, we can run a run an example program on the switch using `./run_switchd.sh --arch tofino2 -p <example_name>`. This should immediately run `bfshell` and you can interact with the program right away. Alternatively, you can `./run_bfshell` from another terminal.

You can also run the tests for the example using `./run_p4_tests.sh --arch=tf2 --target=hw -p <example_name>`. Depending on the example, the test can verify whether you are able to write/modify/delete table entries. Be aware that most test cases **will fail** since they are using the P4 Packet Test Framework (PTF), which only generates packets on virtual interfaces.


## Rewriting a p4 program from v1model to t2na

The load balancer program we are looking to deploy was written vor the V1Model architecture. The Tofino 2 switch runs the Tofino 2 Native Architecture (T2NA), which has quite some differences to V1Model, mainly the pipeline setup.

In order to rewrite the program, we did the following:
1. Adjusted the pipeline to be (IngressParser -> IngressDeparser -> Ingress -> EgressParser -> Egress -> EgressDeparer)
2. Adjusted parsers to properly parse tofino-specific metadata.
3. Updated the hashing method to the T2NA version.
4. Rewrote some multi-step calculations to ensure the program compiles.
5. Rewrote set_nhop to use only compile-known values (TODO: find a better solution for thiss) 

## Writing a control plane
TODO

## Troubleshooting

### run_switchd

- If there is an error connecting to the device e.g. something like `/dev/fpga0` not found, you might have to load the required kernel modules using [load_kernel_modules.sh](load_kernel_modules.sh).

### bfshell

- If you cannot access the program nodes in `bfrt_python`, just do `CTRL + C` and re-run it.
