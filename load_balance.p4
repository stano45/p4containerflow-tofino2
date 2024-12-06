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

const bit<16> ECMP_BASE = 1;
const bit<16> ECMP_COUNT = 2;

struct metadata_t {
    // bit<16> checksum_ipv4;
    // bit<16> checksum_tcp_udp;
    bit<16> tcp_length;

    bool should_update_ipv4_checksum;
    bool should_update_tcp_udp_checksum;
    bool checksum_err_ipv4_igprs;

    bit<16> hash;
    bit<16> bucket;
    bit<16> ecmp_select;
    bool to_client;
    bit<9>  egress_port;
    
    bit<16> ipv4_header_len;
};

parser SwitchIngressParser(
        packet_in pkt,
        out header_t hdr,
        out metadata_t ig_md,
        out ingress_intrinsic_metadata_t ig_intr_md) {

    TofinoIngressParser() tofino_parser;
    Checksum() ipv4_checksum;
    // Checksum() tcp_checksum;
    // Checksum() udp_checksum;

    state start {
        tofino_parser.apply(pkt, ig_intr_md);
        transition parse_ethernet;
    }

    state parse_ethernet {
        pkt.extract(hdr.ethernet);
        transition select(hdr.ethernet.ether_type) {
            ETHERTYPE_IPV4 : parse_ipv4;
            default : reject;
        }
    }

    state parse_ipv4 {
        pkt.extract(hdr.ipv4);
        ipv4_checksum.add(hdr.ipv4);
        ig_md.checksum_err_ipv4_igprs = ipv4_checksum.verify();

        // tcp_checksum.subtract({ hdr.ipv4.src_addr, hdr.ipv4.dst_addr });
        // udp_checksum.subtract({ hdr.ipv4.src_addr, hdr.ipv4.dst_addr });

        transition select(hdr.ipv4.protocol) {
            IP_PROTOCOLS_TCP : parse_tcp;
            // IP_PROTOCOLS_UDP : parse_udp;
            default : accept;
        }
    }

    state parse_tcp {
        pkt.extract(hdr.tcp);

        // tcp_checksum.subtract_all_and_deposit(ig_md.checksum_tcp_udp);
        // tcp_checksum.subtract({hdr.tcp.checksum});
        // tcp_checksum.subtract({hdr.tcp.src_port, hdr.tcp.dst_port});

        transition accept;
    }

//     state parse_udp {
//         pkt.extract(hdr.udp);

//         // udp_checksum.subtract_all_and_deposit(ig_md.checksum_tcp_udp);
//         // udp_checksum.subtract({hdr.udp.checksum});
//         // udp_checksum.subtract({hdr.udp.src_port, hdr.udp.dst_port});

//         transition accept;
//     }
}

control SwitchIngress(
        inout header_t hdr,
        inout metadata_t ig_md,
        in ingress_intrinsic_metadata_t ig_intr_md,
        in ingress_intrinsic_metadata_from_parser_t ig_prsr_md,
        inout ingress_intrinsic_metadata_for_deparser_t ig_dprsr_md,
        inout ingress_intrinsic_metadata_for_tm_t ig_tm_md) {

    Hash<bit<16>>(HashAlgorithm_t.CRC16) hash;
    BypassEgress() bypass_egress;

    action update_ipv4_checksum(bool update) {
        ig_md.should_update_ipv4_checksum = update; 
    }
    
    action update_tcp_udp_checksum(bool update) {
        ig_md.should_update_tcp_udp_checksum = update; 
    }

    action update_checksum(bool update) {
        update_ipv4_checksum(update);
        update_tcp_udp_checksum(update);
    }


    action compute_hash() {
        ig_md.hash = hash.get({
            hdr.ipv4.src_addr,
            hdr.ipv4.dst_addr,
            hdr.ipv4.protocol,
            hdr.tcp.src_port,
            hdr.tcp.dst_port
        });

        // Use bitwise AND instead of modulo
        // hash % 2^n == hash & (2^n - 1)
        // Adjust bucket count as a power of 2 (e.g. 2^1, 2^2...)
        ig_md.bucket = ig_md.hash & (ECMP_COUNT - 1);
    }

    action compute_ecmp_offset() {
        ig_md.ecmp_select = ig_md.bucket + ECMP_BASE;
    }

    action set_rewrite_src(bit<32> new_src) {
        // Rewrite with load balancer IP,
        // so that the client can receive the packet.
        // (The client only knows the load balancer address.)
        hdr.ipv4.src_addr = new_src;
        // Index 0 is the client.
        ig_md.to_client = true;
        ig_md.ecmp_select = 0;
    }


    action set_ecmp_nhop(bit<32> nhop_ipv4, bit<9> port) {
        hdr.ipv4.dst_addr = nhop_ipv4;
        ig_tm_md.ucast_egress_port = port;
    }

    action set_ttl() {
        hdr.ipv4.ttl = hdr.ipv4.ttl - 1;
    }

    action store_ihl() {
        ig_md.ipv4_header_len = (bit<16>)(hdr.ipv4.ihl);
    }

    action calculate_ipv4_header_len() {
        // IHL (internet header length) specifies number of 32-bit words in header.
        // We want header length in bytes, so the calculation is:
        // (IHL * 32) / 8
        // Which simplifies to:
        // IHL * 4. 
        ig_md.ipv4_header_len = ig_md.ipv4_header_len * 4;
    }

    action set_tcp_len() {
        ig_md.tcp_length = hdr.ipv4.total_len - ig_md.ipv4_header_len;
    }

    action drop() {
        ig_dprsr_md.drop_ctl = 0x1;
    }

    table ecmp_group {
        key = {
            hdr.ipv4.dst_addr: lpm;
        }
        actions = {
            // Case 1: Packet sent to client,
            // rewrite IP to load balancer IP.
            set_rewrite_src;
            // Case 2: Continue in pipeline, do nothing here.
            NoAction;
            // Default: Unrecognized host ip, drop packet.
            drop;
        }
        const default_action = drop;
        size = 1024;
    }

    table ecmp_nhop {
        key = {
            ig_md.ecmp_select: exact;
        }
        actions = {
            set_ecmp_nhop;
            NoAction;
        }
        const default_action = NoAction;
        size = 1024;
    }

    apply {
        if (hdr.ipv4.isValid() && hdr.ipv4.ttl > 0) {
            // Multi-action pipeline since Tofino only
            // supports a single stage per action. Fixes this compile error:
            // `[--Werror=unsupported] error: add: action spanning multiple stages. 
            // Operations on operand 2 ($tmp3[0..31]) in action set_ecmp_select
            // require multiple stages for a single action.
            // We currently support only single stage actions.
            // Please rewrite the action to be a single stage action.`

            ig_md.to_client = false;
            
            // Step 1: Apply ECMP group logic.
            ecmp_group.apply();

            if (!ig_md.to_client) {
                // Step 2: Compute hash and bucket.
                compute_hash();

                // Step 3: Compute ECMP offset.
                compute_ecmp_offset();
            }

            // Step 4: Set next-hop IP & egress port.
            ecmp_nhop.apply();

            // Step 5: Checksum
            // Detect checksum errors in the ingress parser and tag the packets
            if (ig_md.checksum_err_ipv4_igprs) {
                hdr.ethernet.dst_addr = 0x0000deadbeef;
            }
            
            set_ttl();
            store_ihl();
            calculate_ipv4_header_len();
            set_tcp_len();
            update_checksum(true);

            // Nothing to be done for egress, skip it completely.
            bypass_egress.apply(ig_tm_md);
        }
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
    // Checksum() udp_checksum;

    apply {
        // Updating and checking of the checksum is done in the deparser.
        // Checksumming units are only available in the parser sections of 
        // the program.
        // if (ig_md.should_update_ipv4_checksum) {
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
        // }
        // if (ig_md.should_update_tcp_udp_checksum) {
            hdr.tcp.checksum = tcp_checksum.update({
                hdr.ipv4.src_addr,
                hdr.ipv4.dst_addr,
                8w0,
                hdr.ipv4.protocol,
                ig_md.tcp_length,
                hdr.tcp.src_port,
                hdr.tcp.dst_port,
                hdr.tcp.seq_no,
                hdr.tcp.ack_no,
                hdr.tcp.data_offset,
                hdr.tcp.res,
                hdr.tcp.flags,
                hdr.tcp.window,
                16w0,
                hdr.tcp.urgent_ptr
            });
    // hdr.tcp.checksum = tcp_checksum.update({
    //             32w0,
    //             32w0,
    //             8w0,
    //             hdr.ipv4.protocol,
    //             ig_md.tcp_length,
    //             hdr.tcp.src_port,
    //             hdr.tcp.dst_port,
    //             hdr.tcp.seq_no,
    //             hdr.tcp.ack_no,
    //             hdr.tcp.data_offset,
    //             hdr.tcp.res,
    //             hdr.tcp.flags,
    //             hdr.tcp.window,
    //             16w0,
    //             hdr.tcp.urgent_ptr
    //         });
            // hdr.udp.checksum = udp_checksum.update(data = {
            //     hdr.ipv4.src_addr,
            //     hdr.udp.src_port,
            //     hdr.ipv4.dst_addr,
            //     hdr.udp.dst_port,
            //     ig_md.checksum_tcp_udp
            // }, zeros_as_ones = true);
            // UDP specific checksum handling
        // }
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
