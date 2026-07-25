"""
ArchiMate Exchange Format XML → EAKG Ontology (RDF/OWL) Translator
==================================================================

Translates an ArchiMate Model Exchange File Format (v3.1) XML file
into an Enterprise Architecture Knowledge Graph (EAKG) using the
construction rules defined in:

  - "Translation Rules: ArchiMate to Semantic Web Ontology (RDF/OWL)"
  - The base pattern file  eakg_base_pattern_final.ttl

Usage:
    python archimate_to_eakg.py <input.xml> [output.ttl]

If no output path is given, the result is written to  eakg_output.ttl
in the same directory as the input file.
"""

import sys
import os
import xml.etree.ElementTree as ET
from collections import defaultdict

from rdflib import Graph, Namespace, URIRef, Literal, RDF, RDFS, OWL, XSD

# ── User-configurable ────────────────────────────────────────────
# Name of the base Turtle file (must be in the same directory as this script)
BASE_TTL_FILENAME = "eakg_base_pattern.ttl"


# ─────────────────────────────────────────────────────────────────
# 1. SETUP & UTILITIES
# ─────────────────────────────────────────────────────────────────

# --- Namespaces -------------------------------------------------------

EX = Namespace("http://www.example.org/eakg#")
ARCHIMATE = Namespace("http://www.opengroup.org/xsd/archimate/3.0#")

# ArchiMate exchange-format XML namespaces
ARCHIMATE_XML_NS = "http://www.opengroup.org/xsd/archimate/3.0/"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
XML_NSMAP = {
    "am": ARCHIMATE_XML_NS,
    "xsi": XSI_NS,
}


# --- Lookup Tables ----------------------------------------------------

# ArchiMate primitive type strings  →  XSD datatype URIs
ARCHIMATE_TYPE_TO_XSD = {
    "string":  XSD.string,
    "boolean": XSD.boolean,
    "integer": XSD.integer,
    "float":   XSD.float,
    "date":    XSD.date,
    "time":    XSD.time,
    "number":  XSD.decimal,
}


def resolve_archimate_class(xsi_type_value: str) -> URIRef:
    """
    Turn an xsi:type attribute value (e.g. 'BusinessActor') into
    an  archimate:  URIRef  (e.g. archimate:BusinessActor).
    """
    return ARCHIMATE[xsi_type_value]


# --- Central ID → IRI helper -----------------------------------------

def id_to_iri(identifier: str) -> URIRef:
    """
    Convert any ArchiMate identifier string to an IRI in the  ex:  namespace.
    Used uniformly for property-definitions, elements, relationships,
    and concatenated property-instance IDs.
    """
    return EX[identifier]


# ─────────────────────────────────────────────────────────────────
# 2. PASS 1 — Property Definitions  (Rule 3 – Definition Half)
# ─────────────────────────────────────────────────────────────────

def pass1_property_definitions(root, graph):
    """
    Parse  <propertyDefinitions>  and populate the graph with
    ex:PropertyDefinition nodes.

    Returns a dict:  { definition_id: (key_name, xsd_datatype_uriref) }
    """
    propdef_dict = {}

    container = root.find(f"{{{ARCHIMATE_XML_NS}}}propertyDefinitions")
    if container is None:
        return propdef_dict

    for pdef in container.findall(f"{{{ARCHIMATE_XML_NS}}}propertyDefinition"):
        def_id = pdef.get("identifier")
        def_iri = id_to_iri(def_id)

        # Name / key — first <name> child
        name_el = pdef.find(f"{{{ARCHIMATE_XML_NS}}}name")
        key_name = name_el.text.strip() if name_el is not None and name_el.text else ""

        # Data type — attribute "type"
        type_str = (pdef.get("type") or "string").lower()
        xsd_dt = ARCHIMATE_TYPE_TO_XSD.get(type_str, XSD.string)

        # Emit triples
        graph.add((def_iri, RDF.type, EX.PropertyDefinition))
        graph.add((def_iri, EX.hasKey, Literal(key_name, datatype=XSD.string)))
        graph.add((def_iri, EX.hasDataType, xsd_dt))

        # Store for later passes
        propdef_dict[def_id] = (key_name, xsd_dt)

    return propdef_dict


# ─────────────────────────────────────────────────────────────────
# 3. SHARED HELPER — Property Instances  (Rule 3 – Instance Half)
# ─────────────────────────────────────────────────────────────────

def emit_property_instances(subject_id, subject_iri, property_elements, propdef_dict, graph):
    """
    For a given subject (element or relationship), process its
    <property propertyDefinitionRef="..."> children and emit the
    tripartite  PropertyInstance  pattern.

    Parameters
    ----------
    subject_id : str
        The raw ArchiMate identifier of the owning element/relation.
    subject_iri : URIRef
        The RDF IRI of the owning element/relation.
    property_elements : list[xml.etree.ElementTree.Element]
        The <property> XML children.
    propdef_dict : dict
        Output of pass1_property_definitions().
    graph : rdflib.Graph
        The target graph.
    """
    # Per-(subject, propdef) ordinal counter
    ordinal_counter = defaultdict(int)

    for prop_el in property_elements:
        propdef_ref = prop_el.get("propertyDefinitionRef")
        if propdef_ref is None:
            continue

        # Look up datatype from Pass 1
        key_name, xsd_dt = propdef_dict.get(propdef_ref, ("", XSD.string))

        # Build ordinal
        ordinal = ordinal_counter[(subject_id, propdef_ref)]
        ordinal_counter[(subject_id, propdef_ref)] += 1

        # Instance IRI:  {subject_id}-{propdef_id}-{ordinal}
        instance_id = f"{subject_id}-{propdef_ref}-{ordinal}"
        instance_iri = id_to_iri(instance_id)

        # Read the literal value
        value_el = prop_el.find(f"{{{ARCHIMATE_XML_NS}}}value")
        raw_value = value_el.text.strip() if value_el is not None and value_el.text else ""

        # Type the instance
        graph.add((instance_iri, RDF.type, EX.PropertyInstance))

        # Link instance → definition
        graph.add((instance_iri, EX.isInstanceOf, id_to_iri(propdef_ref)))

        # Attach the literal value, cast to the correct XSD type
        graph.add((instance_iri, EX.hasValue, Literal(raw_value, datatype=xsd_dt)))

        # Link subject → instance
        graph.add((subject_iri, EX.hasProperty, instance_iri))


# ─────────────────────────────────────────────────────────────────
# 4. PASS 2 — Elements  (Rule 1)
# ─────────────────────────────────────────────────────────────────

def pass2_elements(root, propdef_dict, graph):
    """
    Parse  <elements>  and populate the graph with NamedIndividual
    vertices typed to the closed ArchiMate vocabulary.
    """
    container = root.find(f"{{{ARCHIMATE_XML_NS}}}elements")
    if container is None:
        return

    for elem in container.findall(f"{{{ARCHIMATE_XML_NS}}}element"):
        elem_id = elem.get("identifier")
        elem_iri = id_to_iri(elem_id)

        # Resolve xsi:type  →  archimate: class
        xsi_type = elem.get(f"{{{XSI_NS}}}type")
        if xsi_type:
            archimate_class = resolve_archimate_class(xsi_type)
            graph.add((archimate_class, RDF.type, OWL.Class))
            graph.add((elem_iri, RDF.type, archimate_class))

        # owl:NamedIndividual
        graph.add((elem_iri, RDF.type, OWL.NamedIndividual))

        # rdfs:label  ←  first <name>
        name_el = elem.find(f"{{{ARCHIMATE_XML_NS}}}name")
        if name_el is not None and name_el.text:
            graph.add((elem_iri, RDFS.label,
                        Literal(name_el.text.strip(), datatype=XSD.string)))

        # rdfs:comment  ←  first <documentation>
        doc_el = elem.find(f"{{{ARCHIMATE_XML_NS}}}documentation")
        if doc_el is not None and doc_el.text:
            graph.add((elem_iri, RDFS.comment,
                        Literal(doc_el.text.strip(), datatype=XSD.string)))

        # Property instances
        props = elem.findall(f"{{{ARCHIMATE_XML_NS}}}properties/{{{ARCHIMATE_XML_NS}}}property")
        if not props:
            # Some exporters place <property> directly under <element>
            props = elem.findall(f"{{{ARCHIMATE_XML_NS}}}property")
        emit_property_instances(elem_id, elem_iri, props, propdef_dict, graph)


# ─────────────────────────────────────────────────────────────────
# 5. PASS 3 — Relationships  (Rule 2)
# ─────────────────────────────────────────────────────────────────

def pass3_relationships(root, propdef_dict, graph):
    """
    Parse  <relationships>  and populate the graph with
    owl:ObjectProperty edges typed via rdfs:subPropertyOf.
    """
    container = root.find(f"{{{ARCHIMATE_XML_NS}}}relationships")
    if container is None:
        return

    for rel in container.findall(f"{{{ARCHIMATE_XML_NS}}}relationship"):
        rel_id = rel.get("identifier")
        rel_iri = id_to_iri(rel_id)

        # owl:ObjectProperty
        graph.add((rel_iri, RDF.type, OWL.ObjectProperty))

        # rdfs:subPropertyOf  ←  resolved xsi:type
        xsi_type = rel.get(f"{{{XSI_NS}}}type")
        if xsi_type:
            archimate_rel_type = resolve_archimate_class(xsi_type)
            graph.add((archimate_rel_type, RDF.type, OWL.ObjectProperty))
            graph.add((rel_iri, RDFS.subPropertyOf, archimate_rel_type))

        # rdfs:comment  ←  first <documentation>
        doc_el = rel.find(f"{{{ARCHIMATE_XML_NS}}}documentation")
        if doc_el is not None and doc_el.text:
            graph.add((rel_iri, RDFS.comment,
                        Literal(doc_el.text.strip(), datatype=XSD.string)))

        # Core triple:  source  relIRI  target
        source_id = rel.get("source")
        target_id = rel.get("target")
        if source_id and target_id:
            graph.add((id_to_iri(source_id), rel_iri, id_to_iri(target_id)))

        # Property instances
        props = rel.findall(f"{{{ARCHIMATE_XML_NS}}}properties/{{{ARCHIMATE_XML_NS}}}property")
        if not props:
            props = rel.findall(f"{{{ARCHIMATE_XML_NS}}}property")
        emit_property_instances(rel_id, rel_iri, props, propdef_dict, graph)


# ─────────────────────────────────────────────────────────────────
# 6. MAIN — PIPELINE ORCHESTRATION & OUTPUT
# ─────────────────────────────────────────────────────────────────

def main():
    # --- Resolve file paths -------------------------------------------
    if len(sys.argv) < 2:
        print("Usage:  python archimate_to_eakg.py <input.xml> [output.ttl]")
        sys.exit(1)

    input_xml = sys.argv[1]
    if len(sys.argv) >= 3:
        output_ttl = sys.argv[2]
    else:
        output_ttl = os.path.join(os.path.dirname(input_xml) or ".",
                                  "eakg_output.ttl")

    # Path to the base pattern file (same directory as this script)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    base_ttl = os.path.join(script_dir, BASE_TTL_FILENAME)

    # --- Graph Initialization -----------------------------------------
    g = Graph()
    g.parse(base_ttl, format="turtle")

    # Bind namespace prefixes for clean serialization
    g.bind("ex",        EX)
    g.bind("archimate", ARCHIMATE)
    g.bind("owl",       OWL)
    g.bind("rdf",       RDF)
    g.bind("rdfs",      RDFS)
    g.bind("xsd",       XSD)

    # --- XML Parsing --------------------------------------------------
    tree = ET.parse(input_xml)
    root = tree.getroot()

    # --- Execute pipeline passes --------------------------------------
    print(f"[Pass 1] Parsing property definitions …")
    propdef_dict = pass1_property_definitions(root, g)
    print(f"         Found {len(propdef_dict)} property definition(s).")

    print(f"[Pass 2] Parsing elements …")
    pass2_elements(root, propdef_dict, g)

    print(f"[Pass 3] Parsing relationships …")
    pass3_relationships(root, propdef_dict, g)

    # --- Output -------------------------------------------------------
    print(f"[Output] Serializing graph ({len(g)} triples) → {output_ttl}")
    g.serialize(destination=output_ttl, format="turtle")
    print("[Done]")


if __name__ == "__main__":
    main()
