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
    bit<16> checksum_ipv4_tmp;
    bit<16> checksum_tcp_tmp;
    bit<16> checksum_udp_tmp;

    bool checksum_upd_ipv4;
    bool checksum_upd_tcp;
    bool checksum_upd_udp;

    bool checksum_err_ipv4_igprs;

    // Step 1
    bit<32> hash;
    
    // Step 2
    bit<32> hash_mod_2;

    // Step 3
    bit<32> ecmp_select;
    bool to_client;

    // Step 4
    bit<9>  egress_port;
};

parser SwitchIngressParser(
        packet_in pkt,
        out header_t hdr,
        out metadata_t ig_md,
        out ingress_intrinsic_metadata_t ig_intr_md) {

    TofinoIngressParser() tofino_parser;
    Checksum() ipv4_checksum;
    Checksum() tcp_checksum;
    Checksum() udp_checksum;

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

        tcp_checksum.subtract({hdr.ipv4.src_addr});
        udp_checksum.subtract({hdr.ipv4.src_addr});

        transition select(hdr.ipv4.protocol) {
            IP_PROTOCOLS_TCP : parse_tcp;
            IP_PROTOCOLS_UDP : parse_udp;
            default : accept;
        }
    }

    state parse_tcp {
        // The tcp checksum cannot be verified, since we cannot compute
        // the payload's checksum.
        pkt.extract(hdr.tcp);

        tcp_checksum.subtract({hdr.tcp.checksum});
        tcp_checksum.subtract({hdr.tcp.src_port});
        ig_md.checksum_tcp_tmp = tcp_checksum.get();

        transition accept;
    }

    state parse_udp {
        // The tcp checksum cannot be verified, since we cannot compute
        // the payload's checksum.
        pkt.extract(hdr.udp);

        udp_checksum.subtract({hdr.udp.checksum});
        udp_checksum.subtract({hdr.udp.src_port});
        ig_md.checksum_udp_tmp = udp_checksum.get();

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

    Hash<bit<32>>(HashAlgorithm_t.CRC16) hash;
    BypassEgress() bypass_egress;

    action checksum_upd_ipv4(bool update) {
        ig_md.checksum_upd_ipv4 = update; 
    }
    
    action checksum_upd_tcp(bool update) {
        ig_md.checksum_upd_tcp = update; 
    }

    action checksum_upd_udp(bool update) {
        ig_md.checksum_upd_udp = update; 
    }

    action checksum_upd_ipv4_tcp_udp(bool update) {
        checksum_upd_ipv4(update);
        checksum_upd_tcp(update);
        checksum_upd_udp(update);
    }


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
        ig_md.to_client = true;
        ig_md.ecmp_select = 0;
        checksum_upd_ipv4_tcp_udp(true);
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

            if (!ig_md.to_client) {
                // Step 2: Compute hash and bucket.
                compute_hash_table.apply();

                // Step 3: Compute ECMP select.
                compute_ecmp_table.apply();
            }

            // Step 4: Set next-hop port.
            ecmp_nhop.apply();

            // Step 5: Checksum checks

            // Ensure the UDP checksum is only checked if it was set in the incoming
            // packet or if its a udp special case 
            if (hdr.udp.checksum == 0 && ig_md.checksum_upd_udp) {
                checksum_upd_udp(false);
            }

            // Detect checksum errors in the ingress parser and tag the packets
            if (ig_md.checksum_err_ipv4_igprs) {
                hdr.ethernet.dst_addr = 0x0000deadbeef;
            }

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
    Checksum() udp_checksum;

    apply {
        // Updating and checking of the checksum is done in the deparser.
        // Checksumming units are only available in the parser sections of 
        // the program.
        if (ig_md.checksum_upd_ipv4) {
            hdr.ipv4.hdr_checksum = ipv4_checksum.update(
                {hdr.ipv4.version,
                 hdr.ipv4.ihl,
                 hdr.ipv4.diffserv,
                 hdr.ipv4.total_len,
                 hdr.ipv4.identification,
                 hdr.ipv4.flags,
                 hdr.ipv4.frag_offset,
                 hdr.ipv4.ttl,
                 hdr.ipv4.protocol,
                 hdr.ipv4.src_addr,
                 hdr.ipv4.dst_addr});
        }
        if (ig_md.checksum_upd_tcp) {
            hdr.tcp.checksum = tcp_checksum.update({
                hdr.ipv4.src_addr,
                hdr.tcp.src_port,
                ig_md.checksum_tcp_tmp
            });
        }
        if (ig_md.checksum_upd_udp) {
            hdr.udp.checksum = udp_checksum.update(data = {
                hdr.ipv4.src_addr,
                hdr.udp.src_port,
                ig_md.checksum_udp_tmp
            }, zeros_as_ones = true);
            // UDP specific checksum handling
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
