#include <core.p4>
#if __TARGET_TOFINO__ == 3
#include <t3na.p4>
#elif __TARGET_TOFINO__ == 2
#include <t2na.p4>
#else
#include <tna.p4>
#endif

#include "common/headers.p4"
#include "common/util.p4"

struct metadata_t {
    bool is_lb_packet;
    bool checksum_err_ipv4_igprs;
    bit<16> checksum_tcp_tmp;
    bool checksum_upd_ipv4;
    bool checksum_upd_tcp;
};

parser SwitchIngressParser(
        packet_in pkt,
        out header_t hdr,
        out metadata_t ig_md,
        out ingress_intrinsic_metadata_t ig_intr_md) {

    TofinoIngressParser() tofino_parser;
    Checksum() ipv4_checksum;
    Checksum() tcp_checksum;

    state start {
        tofino_parser.apply(pkt, ig_intr_md);
        transition parse_ethernet;
    }

    state parse_ethernet {
        pkt.extract(hdr.ethernet);

        transition select(hdr.ethernet.ether_type) {
            ETHERTYPE_IPV4 : parse_ipv4;
            ETHERTYPE_ARP  : parse_arp;
            default : reject;
        }
    }

    state parse_arp {
        pkt.extract(hdr.arp);
        transition accept;
    }

    state parse_ipv4 {
        pkt.extract(hdr.ipv4);
        ipv4_checksum.add(hdr.ipv4);
        ig_md.checksum_err_ipv4_igprs = ipv4_checksum.verify();

        tcp_checksum.subtract({hdr.ipv4.src_addr, hdr.ipv4.dst_addr});

        transition select(hdr.ipv4.protocol) {
            IP_PROTOCOLS_TCP : parse_tcp;
            default : accept;
        }
    }

    // Only TCP for now, easily extensible to UDP
    state parse_tcp {
        pkt.extract(hdr.tcp);
        tcp_checksum.subtract({hdr.tcp.checksum});
        tcp_checksum.subtract_all_and_deposit(ig_md.checksum_tcp_tmp);

        transition accept;
    }
}

control SwitchIngress(
        inout header_t hdr,
        inout metadata_t ig_md,
        in ingress_intrinsic_metadata_t ig_intr_md,
        in ingress_intrinsic_metadata_from_parser_t ig_prsr_md,
        inout ingress_intrinsic_metadata_for_deparser_t ig_dprsr_md,
        inout ingress_intrinsic_metadata_for_tm_t ig_tm_md) {

    Hash<bit<16>>(HashAlgorithm_t.CRC16) sel_hash;
    // Action Profile Size = max group size x max number of groups
    ActionProfile(4) action_selector_ap;
    ActionSelector(action_selector_ap, // action profile
                   sel_hash, // hash extern
                   SelectorMode_t.FAIR, // Selector algorithm
                   4, // max group size
                   1 // max number of groups
                   ) action_selector;
    BypassEgress() bypass_egress;

    action checksum_upd_ipv4(bool update) {
        ig_md.checksum_upd_ipv4 = update; 
    }
    
    action checksum_upd_tcp(bool update) {
        ig_md.checksum_upd_tcp = update; 
    }
    
    action checksum_upd_ipv4_tcp(bool update) {
        checksum_upd_ipv4(update);
        checksum_upd_tcp(update);
    }

    action set_rewrite_dst(bit<32> new_dst) {
        // Rewrite with load balancer IP,
        // so that the client can receive the packet.
        // (The client only knows the load balancer address.)
        hdr.ipv4.dst_addr = new_dst;
        ig_md.is_lb_packet = true;

        // Source address changed, 
        // mark to update checksum in deparser
        checksum_upd_ipv4_tcp(true);
    }

    action set_rewrite_src(bit<32> new_src) {
        // Rewrite with load balancer IP,
        // so that the client can receive the packet.
        // (The client only knows the load balancer address.)
        hdr.ipv4.src_addr = new_src;

        // Source address changed, 
        // mark to update checksum in deparser
        checksum_upd_ipv4_tcp(true);
    }

    action set_egress_port(bit<9> port) {
        ig_tm_md.ucast_egress_port = port;
    }

    action set_egress_port_with_mac(bit<9> port, mac_addr_t dst_mac) {
        ig_tm_md.ucast_egress_port = port;
        hdr.ethernet.dst_addr = dst_mac;
    }

    table client_snat {
        key = {
            hdr.tcp.src_port: exact;
        }
        actions = {
            set_rewrite_src;
            NoAction;
        }
        const default_action = NoAction;
        size = 1024;
    }

    table node_selector {
        key = {
            hdr.ipv4.dst_addr: exact;
            hdr.ipv4.src_addr: selector;
            hdr.ipv4.dst_addr: selector;
            hdr.ipv4.protocol: selector;
            hdr.tcp.src_port:  selector;
            hdr.tcp.dst_port:  selector;
        }
        actions = {
            set_rewrite_dst;
            NoAction;
        }
        const default_action = NoAction;
        size = 1024;
        implementation = action_selector;
    }

    table arp_forward {
        key = {
            hdr.arp.target_proto_addr: exact;
        }
        actions = {
            set_egress_port;
            NoAction;
        }
        const default_action = NoAction;
        size = 64;
    }

    table forward {
        key = {
            hdr.ipv4.dst_addr: exact;
        }
        actions = {
            set_egress_port;
            set_egress_port_with_mac;
            NoAction;
        }
        default_action = NoAction;
        size = 1024;
    }


    apply {
        // Handle ARP: forward based on target protocol address
        if (hdr.arp.isValid()) {
            arp_forward.apply();
            bypass_egress.apply(ig_tm_md);
            return;
        }

        // Ignore invalid IPv4 packets
        if (!hdr.ipv4.isValid() || hdr.ipv4.ttl < 1) {
            return;
        }
        
        ig_md.is_lb_packet = false;
        node_selector.apply();

        if (!ig_md.is_lb_packet) {
            client_snat.apply();
        }

        forward.apply();

        // Detect checksum errors in the ingress parser and tag the packets
        if (ig_md.checksum_err_ipv4_igprs) {
            hdr.ethernet.dst_addr = 0x0000deadbeef;
        }

        // Nothing to be done for egress (as of yet), skip it completely.
        bypass_egress.apply(ig_tm_md);
    }
}

control SwitchIngressDeparser(packet_out pkt,
                              inout header_t hdr,
                              in metadata_t ig_md,
                              in ingress_intrinsic_metadata_for_deparser_t 
                                ig_intr_dprsr_md
                              ) {

    Checksum() ipv4_checksum;
    Checksum() tcp_checksum;

    apply {
        // Updating and checking of the checksum is done in the deparser.
        // Checksumming units are only available in the parser sections of 
        // the program.
        if (ig_md.checksum_upd_ipv4) {
            hdr.ipv4.hdr_checksum = ipv4_checksum.update({
                hdr.ipv4.version,
                hdr.ipv4.ihl,
                hdr.ipv4.diffserv,
                hdr.ipv4.total_len,
                hdr.ipv4.identification,
                hdr.ipv4.flags,
                hdr.ipv4.frag_offset,
                hdr.ipv4.ttl,
                hdr.ipv4.protocol,
                hdr.ipv4.src_addr,
                hdr.ipv4.dst_addr
            });
        }
        if (ig_md.checksum_upd_tcp) {
            // We only ever change src and dst addr
            hdr.tcp.checksum = tcp_checksum.update({
                hdr.ipv4.src_addr,
                hdr.ipv4.dst_addr,
                ig_md.checksum_tcp_tmp
            });
        }
        pkt.emit(hdr);
    }
}

Pipeline(SwitchIngressParser(),
         SwitchIngress(),
         SwitchIngressDeparser(),
         EmptyEgressParser(),
         EmptyEgress(),
         EmptyEgressDeparser()) pipe;

Switch(pipe) main;
