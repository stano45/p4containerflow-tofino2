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
  
  config.vm.synced_folder ".", "/home/vagrant/p4containerflow-tofino2",
    rsync__exclude: [
      "open-p4studio/build/",
      "open-p4studio/install/",
      ".git/"
    ]
  
  # Basic provisioning - install dependencies only
  config.vm.provision "shell", env: {"DEBIAN_FRONTEND" => "noninteractive"}, inline: <<-SHELL
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
    
    echo "=== Creating setup script ==="
    cat > /home/vagrant/run_setup.sh <<'SETUPSCRIPT'
#!/bin/bash
set -e

export PYTHONUNBUFFERED=1

cd /home/vagrant/p4containerflow-tofino2

echo "=== Cleaning any existing build artifacts ==="
cd open-p4studio && rm -rf build/ install/
cd ..

echo "=== Running make setup ==="
make setup

echo ""
echo "=== Setup complete! ==="
echo "Source the environment with: source ~/setup-open-p4studio.bash"
SETUPSCRIPT
    
    chmod +x /home/vagrant/run_setup.sh
    chown vagrant:vagrant /home/vagrant/run_setup.sh
    
    echo ""
    echo "=========================================="
    echo "VM provisioned successfully!"
    echo "To complete setup, SSH into the VM and run:"
    echo "  vagrant ssh"
    echo "  ./run_setup.sh"
    echo "=========================================="
  SHELL
  
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
