from diagrams import Cluster, Diagram,  Edge
from diagrams.k8s.compute import Pod, StatefulSet
from diagrams.k8s.network import Service
from diagrams.k8s.storage import PV, PVC, StorageClass
from diagrams.oci.network import LoadBalancer

with Diagram("P4-Based Load Balancer in Kubernetes", show=False):
    with Cluster("Cluster"):
        lb = LoadBalancer("Tofino2")
        client = Pod("client")
        apps = []
        apps.append(client >> Edge(style="dashed") >> lb)
        node3 = Pod("node3")
        apps.append(lb >> Edge(style="dashed") >> node3)
        node2 = Pod("node2")
        apps.append(lb >> Edge(style="dashed") >> node2)
        node1 = Pod("node1")
        apps.append(lb >> Edge(style="dashed") >> node1)



with Diagram("Load Balancing to nodes 1 and 2", show=False):
    with Cluster("Cluster"):
        lb = LoadBalancer("Tofino2")
        client = Pod("client")
        apps = []
        apps.append(client >> Edge(color="green", style="dashed") >> lb)
        
        node3 = Pod("node3")
        apps.append(lb >> Edge(style="dashed") >> node3)
        node2 = Pod("node2")
        apps.append(lb >> Edge(color="green", style="dashed") >> node2)
        node1 = Pod("node1")
        apps.append(lb >> Edge(color="green", style="dashed") >> node1)


with Diagram("Migrate from node1 to node3", show=False):
    with Cluster("Cluster"):
        lb = LoadBalancer("Tofino2")
        client = Pod("client")
        apps = []
        apps.append(client >> Edge(color="green", style="dashed") >> lb)
        
        node3 = Pod("node3")
        apps.append(lb >> Edge(color="green", style="dashed") >> node3)
        node2 = Pod("node2")
        apps.append(lb >> Edge(color="green", style="dashed") >> node2)
        node1 = Pod("node1")
        apps.append(lb >> Edge(color="red", style="dashed") >> node1)
        
        

