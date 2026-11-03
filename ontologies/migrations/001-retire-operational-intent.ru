# Migration 001: retire the graph intent vocabulary from a Fuseki store.
#
# What it removes:
#   - every instance of the retired graph intent class (including the demo
#     individual :intent-core-health-01) with all of its triples;
#   - the class itself and its seven properties (:hasTargetMetric, :hasOperator,
#     :hasTargetValue, :appliesToLink, :stateCategory, :validFrom, :validTo),
#     with their domain, range, label and comment triples;
#   - any other triple that uses one of those seven properties, or that names
#     the class as its object.
#
# Who it is for: a Fuseki store that was loaded with a topology.ttl from before
# the class was retired. The ontology loader only adds triples, so editing
# topology.ttl does not remove them from a store that already has them.
#
# What it does not touch: no :ServiceBinding, :Interface or :TopologicalLink
# triple is deleted. Running it on a store that already lacks the vocabulary
# changes nothing.
#
# How it is run: once per store, only after the operator's go-ahead (phase 1,
# plan 01-09), with the triple counts checked before and after. It contains
# DELETE operations only, in the order below.

PREFIX : <http://example.org/network-ontology#>

# 1. Every triple of any subject typed as the retired class.
DELETE { ?s ?p ?o }
WHERE {
  ?s a :OperationalIntent .
  ?s ?p ?o .
} ;

# 2. Every triple whose subject is the class or one of the seven properties.
DELETE { ?s ?p ?o }
WHERE {
  VALUES ?s { :OperationalIntent :hasTargetMetric :hasOperator :hasTargetValue
              :appliesToLink :stateCategory :validFrom :validTo }
  ?s ?p ?o .
} ;

# 3. Every triple whose predicate is one of the seven properties.
DELETE { ?s ?p ?o }
WHERE {
  VALUES ?p { :hasTargetMetric :hasOperator :hasTargetValue
              :appliesToLink :stateCategory :validFrom :validTo }
  ?s ?p ?o .
} ;

# 4. Every triple whose object is the class.
DELETE { ?s ?p ?o }
WHERE {
  VALUES ?o { :OperationalIntent }
  ?s ?p ?o .
}
