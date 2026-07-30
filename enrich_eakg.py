"""
EAKG Enrichment — Extension Layer Merger
=========================================

Takes a populated EAKG base graph (Turtle) produced by
archimate_to_eakg.py and merges one or more extension files on top
of it.  The merge is a standard RDF graph union: every triple from
the extension is added to the base graph.  Because RDF graphs are
sets of triples, duplicates are silently absorbed and no conflicts
can arise.

Usage:
    python enrich_eakg.py <base_graph.ttl> <extension.ttl> [output.ttl]

If no output path is given, the result is written to
eakg_enriched_output.ttl  in the same directory as the base graph.
"""

import sys
import os

from rdflib import Graph, Namespace, OWL, RDF, RDFS, XSD

# ── Namespaces (kept consistent with archimate_to_eakg.py) ───────
EX = Namespace("http://www.example.org/eakg#")
ARCHIMATE = Namespace("http://www.opengroup.org/xsd/archimate/3.0#")


def main():
    # --- Argument handling --------------------------------------------
    if len(sys.argv) < 3:
        print("Usage:  python enrich_eakg.py <base_graph.ttl> <extension.ttl> [output.ttl]")
        sys.exit(1)

    base_path = sys.argv[1]
    extension_path = sys.argv[2]

    if len(sys.argv) >= 4:
        output_path = sys.argv[3]
    else:
        output_path = os.path.join(
            os.path.dirname(base_path) or ".", "eakg_enriched_output.ttl"
        )

    # --- Load base graph ----------------------------------------------
    print(f"[Load]  Parsing base graph: {base_path}")
    g = Graph()
    g.parse(base_path, format="turtle")
    base_count = len(g)
    print(f"        {base_count} triple(s) in base graph.")

    # --- Load extension -----------------------------------------------
    print(f"[Load]  Parsing extension:  {extension_path}")
    g.parse(extension_path, format="turtle")
    enriched_count = len(g)
    added = enriched_count - base_count
    print(f"        {added} new triple(s) added by extension.")

    # --- Bind prefixes for clean output --------------------------------
    g.bind("ex",        EX)
    g.bind("archimate", ARCHIMATE)
    g.bind("owl",       OWL)
    g.bind("rdf",       RDF)
    g.bind("rdfs",      RDFS)
    g.bind("xsd",       XSD)

    # --- Serialize -----------------------------------------------------
    print(f"[Output] Serializing enriched graph ({enriched_count} triples) → {output_path}")
    g.serialize(destination=output_path, format="turtle")
    print("[Done]")


if __name__ == "__main__":
    main()
