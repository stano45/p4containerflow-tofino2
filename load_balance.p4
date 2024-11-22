#include <t2na.p4>


#define switch_ecmp_hash_width 32
#define SwitchEcmpHashAlgorithm HashAlgorithm_t.CRC16
typedef bit<switch_ecmp_hash_width> switch_ecmp_hash_t;
typedef bit<48> mac_addr_t;
typedef bit<32> ipv4_addr_t;
typedef bit<128> ipv6_addr_t;
typedef bit<16> tcp_port_type_t;


typedef bit<16> ether_type_t;
const ether_type_t ETHERTYPE_IPV4 = 16w0x0800;
const ether_type_t ETHERTYPE_IPV6 = 16w0x86dd;
const ether_type_t ETHERTYPE_VLAN = 16w0x8100;
const ether_type_t ETHERTYPE_QINQ = 16w0x9100;
const ether_type_t ETHERTYPE_MPLS = 16w0x8847;
const ether_type_t ETHERTYPE_LLDP = 16w0x88cc;
const ether_type_t ETHERTYPE_LACP = 16w0x8809;
const ether_type_t ETHERTYPE_NSH = 16w0x894f;
const ether_type_t ETHERTYPE_ROCE = 16w0x8915;
const ether_type_t ETHERTYPE_FCOE = 16w0x8906;
const ether_type_t ETHERTYPE_ETHERNET = 16w0x6658;
const ether_type_t ETHERTYPE_ARP = 16w0x0806;
const ether_type_t ETHERTYPE_VNTAG = 16w0x8926;
const ether_type_t ETHERTYPE_TRILL = 16w0x22f3;

typedef bit<8> ip_protocol_t;
const ip_protocol_t IP_PROTOCOLS_ICMP = 1;
const ip_protocol_t IP_PROTOCOLS_IPV4 = 4;
const ip_protocol_t IP_PROTOCOLS_TCP = 6;
const ip_protocol_t IP_PROTOCOLS_UDP = 17;
const ip_protocol_t IP_PROTOCOLS_IPV6 = 41;
const ip_protocol_t IP_PROTOCOLS_GRE = 47;
const ip_protocol_t IP_PROTOCOLS_ICMPV6 = 58;
const ip_protocol_t IP_PROTOCOLS_EIGRP = 88;
const ip_protocol_t IP_PROTOCOLS_OSPF = 89;
const ip_protocol_t IP_PROTOCOLS_PIM = 103;
const ip_protocol_t IP_PROTOCOLS_VRRP = 112;
const ip_protocol_t IP_PROTOCOLS_MPLS = 137;

typedef bit<9> switch_port_id_t;
/*************************************************************************
*********************** H E A D E R S  ***********************************
*************************************************************************/


header ethernet_t {
    mac_addr_t dst_addr;
    mac_addr_t src_addr;
    ether_type_t ether_type;
}

header ipv4_t {
    bit<4>  version;
    bit<4>  ihl;
    bit<8>  diffserv;
    bit<16> total_len;
    bit<16> identification;
    bit<3>  flags;
    bit<13> frag_offset;
    bit<8>  ttl;
    bit<8>  protocol;
    bit<16> hdr_checksum;
    ipv4_addr_t src_addr;
    ipv4_addr_t dst_addr;
}

header tcp_t {
    tcp_port_type_t src_port;
    tcp_port_type_t dst_port;
    bit<32> seq_no;
    bit<32> ack_no;
    bit<4>  data_offset;
    bit<4>  res;
    bit<1>  cwr;
    bit<1>  ecn;
    bit<1>  urg;
    bit<1>  ack;
    bit<1>  psh;
    bit<1>  rst;
    bit<1>  syn;
    bit<1>  fin;
    bit<16> window;
    bit<16> checksum;
    bit<16> urgent_ptr;
    // error: hdr.tcp: argument cannot contain varbit fields
    // varbit<320>  options;
}

struct metadata_t {
    ipv4_addr_t ecmp_select;
    bit<16> tcp_length;
}

struct header_t {
    ethernet_t ethernet;
    ipv4_t     ipv4;
    tcp_t      tcp;
}


/*************************************************************************
*********************** P A R S E R  ***********************************
*************************************************************************/

parser TofinoIngressParser(
        packet_in pkt,
        out ingress_intrinsic_metadata_t ig_intr_md) {
    state start {
        pkt.extract(ig_intr_md);
        transition select(ig_intr_md.resubmit_flag) {
            1 : parse_resubmit;
            0 : parse_port_metadata;
        }
    }

    state parse_resubmit {
        // Parse resubmitted packet here.
        transition reject;
    }

    state parse_port_metadata {
        pkt.advance(PORT_METADATA_SIZE);
        transition accept; 
    }
}

parser SwitchIngressParser(
        packet_in pkt,
        out header_t hdr,
        out metadata_t ig_md,
        out ingress_intrinsic_metadata_t ig_intr_md) {

    TofinoIngressParser() tofino_parser;

    state start {
        tofino_parser.apply(pkt, ig_intr_md);
        transition parse_ethernet;
    }

    state parse_ethernet {
        pkt.extract(hdr.ethernet);
        transition select (hdr.ethernet.ether_type) {
            ETHERTYPE_IPV4 : parse_ipv4;
            // ETHERTYPE_IPV6 : parse_ipv6;
            default : reject;
        }
    }

    state parse_ipv4 {
        pkt.extract(hdr.ipv4);
        transition select(hdr.ipv4.protocol) {
            IP_PROTOCOLS_TCP: parse_tcp;
            default: accept;
        }
    }

    // state parse_ipv6 {
    //     pkt.extract(hdr.ipv6);
    //      transition select(hdr.ipv6.protocol) {
    //         6: parse_tcp;
    //         default: accept;
    //     }
    // }

    state parse_tcp {
        pkt.extract(hdr.tcp);
        transition accept;
    }
}


/*************************************************************************
************   C H E C K S U M    V E R I F I C A T I O N   *************
*************************************************************************/

// control MyVerifyChecksum(inout header_t hdr, inout metadata_t meta) {
//     apply { }
// }

/*************************************************************************
**************  I N G R E S S   P R O C E S S I N G   *******************
*************************************************************************/




control SwitchIngress(
        inout header_t hdr,
        inout metadata_t ig_md,
        in ingress_intrinsic_metadata_t ig_intr_md,
        in ingress_intrinsic_metadata_from_parser_t ig_prsr_md,
        inout ingress_intrinsic_metadata_for_deparser_t ig_dprsr_md,
        inout ingress_intrinsic_metadata_for_tm_t ig_tm_md) {

    Hash<switch_ecmp_hash_t>(SwitchEcmpHashAlgorithm) ipv4_hash;


    action set_ecmp_select() {
        switch_ecmp_hash_t hash = ipv4_hash.get({
            hdr.ipv4.src_addr,
            hdr.ipv4.dst_addr,
            hdr.ipv4.protocol,
            hdr.tcp.src_port,
            hdr.tcp.dst_port
        });
        
        ipv4_addr_t ecmp_index = hash % 2;
        ig_md.ecmp_select = ecmp_index;
    }
    action set_rewrite_src(ipv4_addr_t new_src) {
        hdr.ipv4.src_addr = new_src;
        ig_md.ecmp_select = 0;
    }
    action set_nhop(mac_addr_t nhop_dmac, ipv4_addr_t nhop_ipv4, switch_port_id_t port) {
        hdr.ethernet.dst_addr = nhop_dmac;
        hdr.ipv4.dst_addr = nhop_ipv4;
        ig_tm_md.ucast_egress_port = port;


        hdr.ipv4.ttl = hdr.ipv4.ttl - 1;
        bit<16> ihl = (bit<16>)hdr.ipv4.ihl;
        bit<16> res = ihl << 2;
        bit<16> len = hdr.ipv4.total_len;
        bit<16> final_len = len - res;
        ig_md.tcp_length = len;
    }
    table ecmp_group {
        key = {
            hdr.ipv4.dst_addr: exact;
        }
        actions = {
            NoAction;
            set_ecmp_select;
            set_rewrite_src;
        }
        size = 1024;
        default_action = NoAction;
    }
    table ecmp_nhop {
        key = {
            ig_md.ecmp_select: exact;
        }
        actions = {
            NoAction;
            set_nhop;
        }
        size = 4;
        default_action = NoAction;
    }
    apply {
        if (hdr.ipv4.isValid() && hdr.ipv4.ttl > 0) {
            ecmp_group.apply();
            ecmp_nhop.apply();
        }
    }
}

control SwitchIngressDeparser(
        packet_out pkt,
        inout header_t hdr,
        in metadata_t ig_md,
        in ingress_intrinsic_metadata_for_deparser_t ig_intr_dprsr_md) {
    apply { }
}

// ---------------------------------------------------------------------------
// Egress parser
// ---------------------------------------------------------------------------

parser TofinoEgressParser(
        packet_in pkt,
        out egress_intrinsic_metadata_t eg_intr_md) {
    state start {
        pkt.extract(eg_intr_md);
        transition accept;
    }
}


parser SwitchEgressParser(
        packet_in pkt,
        out header_t hdr,
        out metadata_t eg_md,
        out egress_intrinsic_metadata_t eg_intr_md) {

    TofinoEgressParser() tofino_parser;

    state start {
        tofino_parser.apply(pkt, eg_intr_md);
        transition parse_ethernet;
    }

    state parse_ethernet {
        pkt.extract(hdr.ethernet);
        transition select (hdr.ethernet.ether_type) {
            ETHERTYPE_IPV4 : parse_ipv4;
            default : reject;
        }
    }

    state parse_ipv4 {
        pkt.extract(hdr.ipv4);
        transition accept;
    }
}

    /*************************************************************************
****************  E G R E S S   P R O C E S S I N G   *******************
*************************************************************************/



control SwitchEgress(
        inout header_t hdr,
        inout metadata_t meta,
        in egress_intrinsic_metadata_t eg_intr_md,
        in egress_intrinsic_metadata_from_parser_t eg_intr_md_from_prsr,
        inout egress_intrinsic_metadata_for_deparser_t eg_intr_md_for_dprs,
        inout egress_intrinsic_metadata_for_output_port_t eg_intr_md_for_oport) {


    action rewrite_mac(mac_addr_t smac) {
        hdr.ethernet.src_addr = smac;
    }
    action drop() {
        eg_intr_md_for_dprs.drop_ctl = 0x1; // Drop packet.
    }
    table send_frame {
        key = {
            eg_intr_md.egress_port: exact;
        }
        actions = {
            rewrite_mac;
            drop;
        }
        size = 256;
    }
    apply {
        send_frame.apply();
    }
}


/*************************************************************************
*************   C H E C K S U M    C O M P U T A T I O N   **************
*************************************************************************/

// control MyComputeChecksum(inout headers hdr, inout metadata meta) {
//      apply {
//         update_checksum(
//             hdr.ipv4.isValid(),
//             {
//                 hdr.ipv4.version,
//                 hdr.ipv4.ihl,
//                 hdr.ipv4.diffserv,
//                 hdr.ipv4.total_len,
//                 hdr.ipv4.identification,
//                 hdr.ipv4.flags,
//                 hdr.ipv4.frag_offset,
//                 hdr.ipv4.ttl,
//                 hdr.ipv4.protocol,
//                 hdr.ipv4.src_addr,
//                 hdr.ipv4.dst_addr 
//             },
//             hdr.ipv4.hdr_checksum,
//             HashAlgorithm.csum16
//         );

//         update_checksum_with_payload(
//             hdr.tcp.isValid(),
//             {   
//                 hdr.ipv4.src_addr,
//                 hdr.ipv4.dst_addr,
//                 8w0,
//                 hdr.ipv4.protocol,
//                 meta.tcp_length,
//                 hdr.tcp.src_port,
//                 hdr.tcp.dst_port,
//                 hdr.tcp.seq_no,
//                 hdr.tcp.ack_no,
//                 hdr.tcp.data_offset,
//                 hdr.tcp.res,
//                 hdr.tcp.cwr,
//                 hdr.tcp.ecn,
//                 hdr.tcp.urg,
//                 hdr.tcp.ack,
//                 hdr.tcp.psh,
//                 hdr.tcp.rst,
//                 hdr.tcp.syn,
//                 hdr.tcp.fin,
//                 hdr.tcp.window,
//                 16w0,
//                 hdr.tcp.urgent_ptr
//             },
//             hdr.tcp.checksum,
//             HashAlgorithm.csum16
//         );
//     }
// }



// ---------------------------------------------------------------------------
// Egress Deparser
// ---------------------------------------------------------------------------
control SwitchEgressDeparser(
        packet_out pkt,
        inout header_t hdr,
        in metadata_t eg_md,
        in egress_intrinsic_metadata_for_deparser_t ig_intr_dprs_md) {
    apply {
        pkt.emit(hdr.ethernet);
        pkt.emit(hdr.ipv4);
        pkt.emit(hdr.tcp);
    }
}

/*************************************************************************
***********************  S W I T C H  *******************************
*************************************************************************/

Pipeline(SwitchIngressParser(),
         SwitchIngress(),
         SwitchIngressDeparser(),
         SwitchEgressParser(),
         SwitchEgress(),
         SwitchEgressDeparser()) pipe;

Switch(pipe) main;
