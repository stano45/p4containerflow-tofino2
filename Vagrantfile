# -*- mode: ruby -*-
# vi: set ft=ruby :

Vagrant.configure("2") do |config|
  config.vm.box = "ubuntu/jammy64"

  # Resize disk to 50GB (requires vagrant-disksize plugin)
  config.disksize.size = '50GB'

  config.vm.provider "virtualbox" do |vb|
    vb.name = "p4containerflow-tofino2"
    vb.memory = "32768"
    vb.cpus = 10
  end

  # Provision as root - install dependencies, clone repo, and run setup
  config.vm.provision "shell", env: {"DEBIAN_FRONTEND" => "noninteractive", "PYTHONUNBUFFERED" => "1"}, inline: <<-SHELL
    set -e

    echo "=== Updating system packages ==="
    apt-get update
    apt-get upgrade -y

    echo "=== Installing basic dependencies ==="
    apt-get install -y \
      git \
      build-essential \
      python3 \
      python3-pip \
      python3-dev

    echo "=== Cloning repository ==="
    if [ ! -d "/home/vagrant/p4containerflow-tofino2" ]; then
      git clone https://github.com/stano45/p4containerflow-tofino2.git /home/vagrant/p4containerflow-tofino2
      chown -R vagrant:vagrant /home/vagrant/p4containerflow-tofino2
    else
      echo "Repository already exists, pulling latest changes..."
      cd /home/vagrant/p4containerflow-tofino2
      git pull
      chown -R vagrant:vagrant /home/vagrant/p4containerflow-tofino2
    fi

    cd /home/vagrant/p4containerflow-tofino2

    echo "=== Running model setup ==="
    make setup-model PROFILE=profiles/tofino2-model.yaml

    echo "=== Creating environment setup script for vagrant user ==="
    cd open-p4studio && ./create-setup-script.sh > /home/vagrant/setup-open-p4studio.bash
    chown vagrant:vagrant /home/vagrant/setup-open-p4studio.bash

    echo ""
    echo "=== Setup complete! ==="
    echo "Environment will be auto-sourced on login."
  SHELL

  # Add auto-source to bashrc
  config.vm.provision "shell", privileged: false, inline: <<-SHELL
    if ! grep -q "setup-open-p4studio.bash" ~/.bashrc; then
      echo "" >> ~/.bashrc
      echo "# Auto-source P4 Studio environment" >> ~/.bashrc
      echo "if [ -f ~/setup-open-p4studio.bash ]; then" >> ~/.bashrc
      echo "    source ~/setup-open-p4studio.bash" >> ~/.bashrc
      echo "fi" >> ~/.bashrc
    fi
  SHELL
end
