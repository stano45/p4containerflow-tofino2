#include <t2na.p4>


#define switch_ecmp_hash_width 16
#define SwitchEcmpHashAlgorithm HashAlgorithm_t.CRC16
typedef bit<switch_ecmp_hash_width> switch_ecmp_hash_t;
#define ETHERTYPE_IPV4 0x0800

/*************************************************************************
*********************** H E A D E R S  ***********************************
*************************************************************************/


struct empty_metadata_t {}

struct empty_header_t {}


header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

header ipv4_t {
    bit<4>  version;
    bit<4>  ihl;
    bit<8>  diffserv;
    bit<16> totalLen;
    bit<16> identification;
    bit<3>  flags;
    bit<13> fragOffset;
    bit<8>  ttl;
    bit<8>  protocol;
    bit<16> hdrChecksum;
    bit<32> srcAddr;
    bit<32> dstAddr;
}

header tcp_t {
    bit<16> srcPort;
    bit<16> dstPort;
    bit<32> seqNo;
    bit<32> ackNo;
    bit<4>  dataOffset;
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
    bit<16> urgentPtr;
    // error: hdr.tcp: argument cannot contain varbit fields
    // varbit<320>  options;
}

struct metadata_t {
    bit<14> ecmp_select;
    bit<16> tcpLength;
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

// control MyVerifyChecksum(inout empty_header_t hdr, inout empty_metadata_t meta) {
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
            ipv4_hdr.srcAddr,
            ipv4_hdr.dstAddr,
            ipv4_hdr.protocol,
            tcp_hdr.srcPort,
            tcp_hdr.dstPort
        });
    }
}


    control SwitchIngress(
            inout header_t hdr,
            inout empty_metadata_t ig_md,
           inout  metadata_t meta,
        inout egress_intrinsic_metadata_t eg_intr_md,
            in ingress_intrinsic_metadata_t ig_intr_md,
            in ingress_intrinsic_metadata_from_parser_t ig_prsr_md,
            inout ingress_intrinsic_metadata_for_deparser_t ig_dprsr_md,
            inout ingress_intrinsic_metadata_for_tm_t ig_tm_md) {

        Ipv4Hash() ipv4_hash;


        action set_ecmp_select(bit<16> ecmp_base, bit<32> ecmp_count) {
            switch_ecmp_hash_t hash;
            ipv4_hash.apply(hdr.ipv4, hdr.tcp, hash);
            
            bit<32> hash_val = hash;
            bit<32> ecmp_index = (hash_val % (ecmp_count - ecmp_base)) + ecmp_base;
            
            meta.ecmp_select = ecmp_index;
        }
        action set_rewrite_src(bit<32> new_src) {
            hdr.ipv4.srcAddr = new_src;
            meta.ecmp_select = 0;
        }
        action set_nhop(bit<48> nhop_dmac, bit<32> nhop_ipv4, bit<9> port) {
            hdr.ethernet.dstAddr = nhop_dmac;
            hdr.ipv4.dstAddr = nhop_ipv4;
            eg_intr_md.egress_spec = port;
            hdr.ipv4.ttl = hdr.ipv4.ttl - 1;
            meta.tcpLength = hdr.ipv4.totalLen - (bit<16>)(hdr.ipv4.ihl)*4;
        }
        table ecmp_group {
            key = {
                hdr.ipv4.dstAddr: lpm;
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
                meta.ecmp_select: exact;
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
        hdr.ethernet.srcAddr = smac;
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
//                 hdr.ipv4.totalLen,
//                 hdr.ipv4.identification,
//                 hdr.ipv4.flags,
//                 hdr.ipv4.fragOffset,
//                 hdr.ipv4.ttl,
//                 hdr.ipv4.protocol,
//                 hdr.ipv4.srcAddr,
//                 hdr.ipv4.dstAddr 
//             },
//             hdr.ipv4.hdrChecksum,
//             HashAlgorithm.csum16
//         );

//         update_checksum_with_payload(
//             hdr.tcp.isValid(),
//             {   
//                 hdr.ipv4.srcAddr,
//                 hdr.ipv4.dstAddr,
//                 8w0,
//                 hdr.ipv4.protocol,
//                 meta.tcpLength,
//                 hdr.tcp.srcPort,
//                 hdr.tcp.dstPort,
//                 hdr.tcp.seqNo,
//                 hdr.tcp.ackNo,
//                 hdr.tcp.dataOffset,
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
//                 hdr.tcp.urgentPtr
//             },
//             hdr.tcp.checksum,
//             HashAlgorithm.csum16
//         );
//     }
// }

/*************************************************************************
***********************  D E P A R S E R  *******************************
*************************************************************************/

control SwitchIngressDeparser(packet_out packet, in header_t hdr) {
    apply {
        packet.emit(hdr.ethernet);
        packet.emit(hdr.ipv4);
        packet.emit(hdr.tcp);
    }
}

// Empty egress parser/control blocks
parser EmptyEgressParser(
        packet_in pkt,
        out empty_header_t hdr,
        out empty_metadata_t eg_md,
        out egress_intrinsic_metadata_t eg_intr_md) {
    state start {
        transition accept;
    }
}

control EmptyEgressDeparser(
        packet_out pkt,
        inout empty_header_t hdr,
        in empty_metadata_t eg_md,
        in egress_intrinsic_metadata_for_deparser_t ig_intr_dprs_md) {
    apply {}
}


/*************************************************************************
***********************  S W I T C H  *******************************
*************************************************************************/

Pipeline(SwitchIngressParser(),
         SwitchIngress(),
         SwitchIngressDeparser(),
         EmptyEgressParser(),
         SwitchEgress(),
         EmptyEgressDeparser()) pipe;

Switch(pipe) main;
