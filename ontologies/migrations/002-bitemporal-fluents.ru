# Migration 002: current-state bindings and links become fluents.
#
# What it does: every :ServiceBinding that still carries its goal (:pinnedBound) and its
# placement (:onInterface) directly gets one goal fluent and one placement fluent, and every
# :TopologicalLink that still carries its interface pair (:hasInterface) gets one link fluent.
# Each fluent is valid and recorded from the moment this request runs (one instant, so the
# broker never claims history it did not record): a question about a time before that
# instant has no goal in force and is inconclusive, never answered with today's goal.
#
# What it removes: the old :pinnedBound and :onInterface triples on bindings and the old
# :hasInterface triples on links, in the same request, so each fact has exactly one source.
# It also removes its own scratch triple. Nothing else is deleted.
#
# What it does not touch: no :Interface node, rdfs:label, :forIntent or :forMetric triple is
# changed, and nothing that already is a fluent is read or written.
#
# Who it is for: a store loaded before Phase 2 (current-state bindings and links). A store
# that already holds fluents, or a fresh store, matches nothing and is left as it is.
#
# How it is run: once per store, only after the operator's go-ahead, with the triple counts
# checked before and after (design/bitemporal-fluents.md has the counts and the pre-flight
# duplicate query). It is one request, which Fuseki applies in one transaction. It is
# idempotent: a second run matches nothing, because the old triples are gone. After it,
# the triple count is the count before plus 8 for each binding plus 4 for each link.

PREFIX : <http://example.org/network-ontology#>

# 1. One migration instant. NOW() differs between the operations of one request on both
#    engines, so the instant is stored once in a scratch triple and read back by the rest.
INSERT { :migration-002 :migrationInstant ?t }
WHERE { BIND(NOW() AS ?t) } ;

# 2. Each binding's pinned goal becomes a goal fluent; the old pin on the binding goes.
DELETE { ?b :pinnedBound ?p }
INSERT {
  ?b :hasGoal ?g .
  ?g a :GoalFluent ;
     :pinnedBound ?p ;
     :validStart ?t ;
     :recordedAt ?t .
}
WHERE {
  :migration-002 :migrationInstant ?t .
  ?b a :ServiceBinding ;
     :pinnedBound ?p .
  BIND(IRI(CONCAT(STR(?b), "-goal-m002")) AS ?g)
} ;

# 3. Each binding's placement becomes a placement fluent; the old :onInterface goes.
DELETE { ?b :onInterface ?i }
INSERT {
  ?b :hasPlacement ?f .
  ?f a :PlacementFluent ;
     :onInterface ?i ;
     :validStart ?t ;
     :recordedAt ?t .
}
WHERE {
  :migration-002 :migrationInstant ?t .
  ?b a :ServiceBinding ;
     :onInterface ?i .
  BIND(IRI(CONCAT(STR(?b), "-place-m002")) AS ?f)
} ;

# 4. Each link's interface pair becomes a link fluent; the old :hasInterface triples go.
DELETE { ?l :hasInterface ?i }
INSERT {
  ?f a :LinkFluent ;
     :ofLink ?l ;
     :hasInterface ?i ;
     :validStart ?t ;
     :recordedAt ?t .
}
WHERE {
  :migration-002 :migrationInstant ?t .
  ?l a :TopologicalLink ;
     :hasInterface ?i .
  BIND(IRI(CONCAT(STR(?l), "-fluent-m002")) AS ?f)
} ;

# 5. Drop the scratch triple.
DELETE WHERE { :migration-002 :migrationInstant ?t }
