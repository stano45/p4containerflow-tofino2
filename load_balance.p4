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
    // Step 1
    bit<32> hash;
    
    // Step 2
    bit<32> hash_mod_2;

    // Step 3
    bit<32> ecmp_select;

    // Step 4
    bit<9>  egress_port;
};

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
        transition select(hdr.ethernet.ether_type) {
            ETHERTYPE_IPV4 : parse_ipv4;
            default : reject;
        }
    }

    state parse_ipv4 {
        pkt.extract(hdr.ipv4);
        transition select(hdr.ipv4.protocol) {
            IP_PROTOCOLS_TCP: parse_tcp;
            IP_PROTOCOLS_UDP : parse_udp;
            default : reject;
        }
    }

    state parse_tcp {
        pkt.extract(hdr.tcp);
        transition accept;
    }

    state parse_udp {
        pkt.extract(hdr.udp);
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

    CRCPolynomial<bit<32>>(32w0x04C11DB7, true, false, false, 32w0xFFFFFFFF, 32w0xFFFFFFFF) poly;
    Hash<bit<32>>(HashAlgorithm_t.CUSTOM, poly) hash;

    action compute_hash() {
        ig_md.hash = hash.get({
            hdr.ipv4.src_addr,
            hdr.ipv4.dst_addr,
            hdr.ipv4.protocol,
            hdr.tcp.src_port,
            hdr.tcp.dst_port
        });

        // Use bit-shifting instead of modulo (e.g., for ecmp_count = 2, shift by 1)
        // Adjust bucket count as a power of 2 (e.g. 2^1, 2^2...)
        ig_md.hash_mod_2 = ig_md.hash >> 1;
    }

    action compute_ecmp_select() {
        // Offset by one - index 0 is the client, not buckets.
        ig_md.ecmp_select = ig_md.hash_mod_2 + 1;
    }

    action set_rewrite_src(bit<32> new_src) {
        // Rewrite with load balancer IP,
        // so that the client can receive the packet.
        // (The client only knows the load balancer address.)
        hdr.ipv4.src_addr = new_src;
        // Index 0 is the client.
        ig_md.ecmp_select = 0;
    }

    action set_egress_port(bit<9> port) {
        ig_tm_md.ucast_egress_port = port;
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

    // This table is unused,
    // Just used to pre-compute the hash.
    // See `apply` block for explanation.
    table compute_hash_table {
        actions = {
            compute_hash;
        }
        const default_action = compute_hash;
        size = 1024;
    }

    // This table is unused.
    // Just used to pre-compute the hash.
    // See `apply` block for explanation.
    table compute_ecmp_table {
        actions = {
            compute_ecmp_select;
        }
        const default_action = compute_ecmp_select;
        size = 1024;
    }

    table ecmp_nhop {
        key = {
            ig_md.ecmp_select: exact;
        }
        actions = {
            set_egress_port;
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

            // Step 1: Apply ECMP group logic.
            ecmp_group.apply();

            // Step 2: Compute hash and bucket.
            compute_hash_table.apply();

            // Step 3: Compute ECMP select.
            compute_ecmp_table.apply();

            // Step 4: Set next-hop port.
            ecmp_nhop.apply();
        }
    }

}

control SwitchIngressDeparser(
        packet_out pkt,
        inout header_t hdr,
        in metadata_t ig_md,
        in ingress_intrinsic_metadata_for_deparser_t ig_intr_dprsr_md) {

    apply {
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
