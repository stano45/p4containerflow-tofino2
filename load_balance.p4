#include <t2na.p4>


#define switch_ecmp_hash_width 32
#define SwitchEcmpHashAlgorithm HashAlgorithm_t.CRC16
typedef bit<switch_ecmp_hash_width> switch_ecmp_hash_t;
#define ETHERTYPE_IPV4 0x0800

/*************************************************************************
*********************** H E A D E R S  ***********************************
*************************************************************************/


header ethernet_t {
    bit<48> dst_addr;
    bit<48> src_addr;
    bit<16> ether_type;
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
    bit<32> src_addr;
    bit<32> dst_addr;
}

header tcp_t {
    bit<16> src_port;
    bit<16> dst_port;
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
    bit<32> ecmp_select;
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
            6: parse_tcp;
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



control Ipv4Hash(in ipv4_t ipv4_hdr, in tcp_t tcp_hdr, out switch_ecmp_hash_t hash) {
    @name(".ipv4_hash")
    Hash<switch_ecmp_hash_t>(SwitchEcmpHashAlgorithm) ipv4_hash;

    apply {
        hash = ipv4_hash.get({
            ipv4_hdr.src_addr,
            ipv4_hdr.dst_addr,
            ipv4_hdr.protocol,
            tcp_hdr.src_port,
            tcp_hdr.dst_port
        });
    }
}


control SwitchIngress(
        inout header_t hdr,
        inout metadata_t ig_md,
        out egress_intrinsic_metadata_t eg_intr_md,
        in ingress_intrinsic_metadata_t ig_intr_md,
        in ingress_intrinsic_metadata_from_parser_t ig_prsr_md,
        inout ingress_intrinsic_metadata_for_deparser_t ig_dprsr_md,
        inout ingress_intrinsic_metadata_for_tm_t ig_tm_md) {

    Ipv4Hash() ipv4_hash;


    action set_ecmp_select(bit<32> ecmp_base, bit<32> ecmp_count) {
        switch_ecmp_hash_t hash;
        ipv4_hash.apply(hdr.ipv4, hdr.tcp, hash);
        
        bit<32> hash_val = hash;
        bit<32> ecmp_index = (hash_val % (ecmp_count - ecmp_base)) + ecmp_base;
        
        ig_md.ecmp_select = ecmp_index;
    }
    action set_rewrite_src(bit<32> new_src) {
        hdr.ipv4.src_addr = new_src;
        ig_md.ecmp_select = 0;
    }
    action set_nhop(bit<48> nhop_dmac, bit<32> nhop_ipv4, bit<9> port) {
        hdr.ethernet.dst_addr = nhop_dmac;
        hdr.ipv4.dst_addr = nhop_ipv4;
        eg_intr_md.egress_port = port;
        hdr.ipv4.ttl = hdr.ipv4.ttl - 1;
        ig_md.tcp_length = hdr.ipv4.total_len - (bit<16>)(hdr.ipv4.ihl)*4;
    }
    table ecmp_group {
        key = {
            hdr.ipv4.dst_addr: lpm;
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


    action rewrite_mac(bit<48> smac) {
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


/*************************************************************************
***********************  D E P A R S E R  *******************************
*************************************************************************/



// Empty egress parser/control blocks
parser EmptyEgressParser(
        packet_in pkt,
        out header_t hdr,
        out metadata_t eg_md,
        out egress_intrinsic_metadata_t eg_intr_md) {
    state start {
        transition accept;
    }
}

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
