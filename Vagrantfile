# -*- mode: ruby -*-
# vi: set ft=ruby :

Vagrant.configure("2") do |config|
  config.vm.box = "ubuntu/jammy64"
  
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
  
  config.vm.provision "shell", inline: <<-SHELL
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
    
    echo "=== Changing to project directory ==="
    cd /home/vagrant/p4containerflow-tofino2
    
    echo "=== Cleaning any existing build artifacts ==="
    sudo -u vagrant bash -c "cd open-p4studio && rm -rf build/ install/"
    
    echo "=== Running make setup ==="
    sudo -u vagrant make setup
    
    echo "=== Setup complete! ==="
    echo "To use the environment, run: source ~/setup-open-p4studio.bash"
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
