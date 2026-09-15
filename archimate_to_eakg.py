"""
ArchiMate Exchange Format XML → EAKG Ontology (RDF/OWL) Translator
==================================================================

Translates an ArchiMate Model Exchange File Format (v3.1) XML file
into an Enterprise Architecture Knowledge Graph (EAKG) using the
construction rules defined in:

  - "Translation Rules: ArchiMate to Semantic Web Ontology (RDF/OWL) v2"
  - The base pattern file  eakg_base_pattern.ttl

Usage:
    python archimate_to_eakg.py <input.xml> [output.ttl]

If no output path is given, the result is written to  eakg_output.ttl
in the same directory as the input file.
"""

import re
import sys
import os
import xml.etree.ElementTree as ET

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
XML_NS = "http://www.w3.org/XML/1998/namespace"
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
    Used uniformly for elements and relationships.
    """
    return EX[identifier]


# --- Property name → IRI helper --------------------------------------

def property_name_to_iri(key_name: str) -> URIRef:
    """
    Convert a property definition's name/key string into a valid IRI
    in the  ex:  namespace.

    Sanitises the key by replacing any characters that are not valid
    in an IRI local name with underscores, and ensures it does not
    start with a digit.
    """
    sanitised = re.sub(r"[^A-Za-z0-9_-]", "_", key_name.strip())
    if sanitised and sanitised[0].isdigit():
        sanitised = "_" + sanitised
    if not sanitised:
        sanitised = "_unnamed"
    return EX[sanitised]


# --- Multi-language literal helper ------------------------------------

def emit_lang_literals(subject_iri, predicate, elements, graph):
    """
    For a list of XML elements (e.g. <name> or <documentation>) that may
    carry an xml:lang attribute, emit one triple per element using a
    language-tagged literal.

    Parameters
    ----------
    subject_iri : URIRef
        The RDF subject.
    predicate : URIRef
        The RDF predicate (e.g. RDFS.label, RDFS.comment).
    elements : list[xml.etree.ElementTree.Element]
        The XML children to iterate over.
    graph : rdflib.Graph
        The target graph.
    """
    for el in elements:
        if el.text and el.text.strip():
            lang = el.get(f"{{{XML_NS}}}lang")
            if lang:
                lit = Literal(el.text.strip(), lang=lang)
            else:
                lit = Literal(el.text.strip(), datatype=XSD.string)
            graph.add((subject_iri, predicate, lit))


# ─────────────────────────────────────────────────────────────────
# 2. PASS 1 — Property Definitions  (Rule 3 – Definition Half)
# ─────────────────────────────────────────────────────────────────

def pass1_property_definitions(root, graph):
    """
    Parse  <propertyDefinitions>  and populate the graph with
    owl:DatatypeProperty predicates identified by their name/key.

    Each property definition is declared once as:
        ex:PropertyName  a  owl:DatatypeProperty ;
            rdfs:subPropertyOf  archimate:Property ;
            rdfs:range          xsd:<datatype> .

    Returns a dict:  { definition_id: (property_iri, xsd_datatype_uriref) }
    """
    propdef_dict = {}

    container = root.find(f"{{{ARCHIMATE_XML_NS}}}propertyDefinitions")
    if container is None:
        return propdef_dict

    for pdef in container.findall(f"{{{ARCHIMATE_XML_NS}}}propertyDefinition"):
        def_id = pdef.get("identifier")

        # Name / key — first <name> child
        name_el = pdef.find(f"{{{ARCHIMATE_XML_NS}}}name")
        key_name = name_el.text.strip() if name_el is not None and name_el.text else ""

        # Data type — attribute "type"
        type_str = (pdef.get("type") or "string").lower()
        xsd_dt = ARCHIMATE_TYPE_TO_XSD.get(type_str, XSD.string)

        # Build the property predicate IRI from the key name
        prop_iri = property_name_to_iri(key_name)

        # Emit triples  (Rule 3 v2 – Definition)
        graph.add((prop_iri, RDF.type, OWL.DatatypeProperty))
        graph.add((prop_iri, RDFS.subPropertyOf, ARCHIMATE.Property))
        graph.add((prop_iri, RDFS.range, xsd_dt))

        # Store for later passes:  ArchiMate def id  →  (predicate IRI, xsd datatype)
        propdef_dict[def_id] = (prop_iri, xsd_dt)

    return propdef_dict


# ─────────────────────────────────────────────────────────────────
# 3. SHARED HELPER — Property Values  (Rule 3 – Instance Half)
# ─────────────────────────────────────────────────────────────────

def emit_property_values(subject_iri, property_elements, propdef_dict, graph):
    """
    For a given subject (element or relationship), process its
    <property propertyDefinitionRef="..."> children and emit direct
    triples of the form:

        subject_iri  ex:PropertyName  "value"^^xsd:datatype .

    Parameters
    ----------
    subject_iri : URIRef
        The RDF IRI of the owning element/relation.
    property_elements : list[xml.etree.ElementTree.Element]
        The <property> XML children.
    propdef_dict : dict
        Output of pass1_property_definitions().
    graph : rdflib.Graph
        The target graph.
    """
    for prop_el in property_elements:
        propdef_ref = prop_el.get("propertyDefinitionRef")
        if propdef_ref is None:
            continue

        # Look up predicate IRI and datatype from Pass 1
        prop_iri, xsd_dt = propdef_dict.get(propdef_ref, (None, XSD.string))
        if prop_iri is None:
            continue

        # Read the literal value
        value_el = prop_el.find(f"{{{ARCHIMATE_XML_NS}}}value")
        raw_value = value_el.text.strip() if value_el is not None and value_el.text else ""

        # Emit direct triple  (Rule 3 v2 – Instance)
        graph.add((subject_iri, prop_iri, Literal(raw_value, datatype=xsd_dt)))


# ─────────────────────────────────────────────────────────────────
# 4. PASS 2 — Elements  (Rule 1)
# ─────────────────────────────────────────────────────────────────

def pass2_elements(root, propdef_dict, graph):
    """
    Parse  <elements>  and populate the graph with NamedIndividual
    vertices typed to the closed ArchiMate vocabulary.

    All <name> and <documentation> children are emitted with their
    xml:lang tags as language-tagged literals.
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

        # rdfs:label  ←  ALL <name> children (multi-language)
        name_els = elem.findall(f"{{{ARCHIMATE_XML_NS}}}name")
        emit_lang_literals(elem_iri, RDFS.label, name_els, graph)

        # rdfs:comment  ←  ALL <documentation> children (multi-language)
        doc_els = elem.findall(f"{{{ARCHIMATE_XML_NS}}}documentation")
        emit_lang_literals(elem_iri, RDFS.comment, doc_els, graph)

        # Property values (direct triples)
        props = elem.findall(f"{{{ARCHIMATE_XML_NS}}}properties/{{{ARCHIMATE_XML_NS}}}property")
        if not props:
            # Some exporters place <property> directly under <element>
            props = elem.findall(f"{{{ARCHIMATE_XML_NS}}}property")
        emit_property_values(elem_iri, props, propdef_dict, graph)


# ─────────────────────────────────────────────────────────────────
# 5. PASS 3 — Relationships  (Rule 2)
# ─────────────────────────────────────────────────────────────────

def pass3_relationships(root, propdef_dict, graph):
    """
    Parse  <relationships>  and populate the graph with
    owl:ObjectProperty edges typed via rdfs:subPropertyOf.

    All <name> and <documentation> children are emitted with their
    xml:lang tags as language-tagged literals.

    Schema-fixed XML attributes (e.g. modifier on InfluenceRelationship)
    are emitted as Rule 3 property instances in the archimate: namespace.
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

        # rdfs:label  ←  ALL <name> children (multi-language)  [Rule 2 – Name]
        name_els = rel.findall(f"{{{ARCHIMATE_XML_NS}}}name")
        emit_lang_literals(rel_iri, RDFS.label, name_els, graph)

        # rdfs:comment  ←  ALL <documentation> children (multi-language)
        doc_els = rel.findall(f"{{{ARCHIMATE_XML_NS}}}documentation")
        emit_lang_literals(rel_iri, RDFS.comment, doc_els, graph)

        # Core triple:  source  relIRI  target
        source_id = rel.get("source")
        target_id = rel.get("target")
        if source_id and target_id:
            graph.add((id_to_iri(source_id), rel_iri, id_to_iri(target_id)))

        # Property values (direct triples)
        props = rel.findall(f"{{{ARCHIMATE_XML_NS}}}properties/{{{ARCHIMATE_XML_NS}}}property")
        if not props:
            props = rel.findall(f"{{{ARCHIMATE_XML_NS}}}property")
        emit_property_values(rel_iri, props, propdef_dict, graph)

        # Schema-fixed attributes  [Rule 3 – Special Case]
        # Declared dynamically only when present in the source XML.

        # accessType on Access relationships (e.g. "Read", "Write", "ReadWrite")
        access_type_val = rel.get("accessType")
        if access_type_val is not None:
            graph.add((ARCHIMATE.accessType, RDF.type, OWL.DatatypeProperty))
            graph.add((ARCHIMATE.accessType, RDFS.subPropertyOf, ARCHIMATE.Property))
            graph.add((ARCHIMATE.accessType, RDFS.range, XSD.string))
            graph.add((rel_iri, ARCHIMATE.accessType,
                       Literal(access_type_val, datatype=XSD.string)))

        # isDirected on Association relationships (e.g. "true", "false")
        is_directed_val = rel.get("isDirected")
        if is_directed_val is not None:
            graph.add((ARCHIMATE.isDirected, RDF.type, OWL.DatatypeProperty))
            graph.add((ARCHIMATE.isDirected, RDFS.subPropertyOf, ARCHIMATE.Property))
            graph.add((ARCHIMATE.isDirected, RDFS.range, XSD.boolean))
            graph.add((rel_iri, ARCHIMATE.isDirected,
                       Literal(is_directed_val, datatype=XSD.boolean)))


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
