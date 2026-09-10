/*
 Context Broker LodLive configuration.

 Routes all requests for our ontology's URIs (http://example.org/network-ontology#...)
 through the local jsonp_proxy service in front of Fuseki — see lodlive-proxy/.
 Overlaid onto the stock LodLive image via a docker-compose bind mount, replacing
 js/lodlive.profile.js — see docker-compose.yml.
 */
$.jStorage.set('profile', {
	'connection' : {
		'http://example.org/network-ontology' : {
			description : {
				en : 'Context Broker network topology / intent ontology (via Fuseki)'
			},
			useForInverseSameAs : true,
			endpoint : 'http://localhost:8081/sparql',
			examples : [ {
				label : 'intent-core-health-01',
				uri : 'http://example.org/network-ontology#intent-core-health-01'
			}, {
				label : 'Router-A <-> Router-B link',
				uri : 'http://example.org/network-ontology#link-router-a-router-b'
			}, {
				label : 'Router-A interface',
				uri : 'http://example.org/network-ontology#if-router-a-ge000'
			} ]
		}
	},
	arrows : {
		'http://www.w3.org/2002/07/owl#sameAs' : 'isSameAs',
		'http://purl.org/dc/terms/isPartOf' : 'isPartOf',
		'http://purl.org/dc/elements/1.1/type' : 'isType',
		'http://www.w3.org/1999/02/22-rdf-syntax-ns#type' : 'isType'
	},
	uriSubstitutor : [],

	'default' : {
		sparql : {
			allClasses : 'SELECT DISTINCT ?object WHERE {[] a ?object}',
			findSubject : 'SELECT DISTINCT ?subject WHERE { {?subject a <{CLASS}>;<http://www.w3.org/2000/01/rdf-schema#label> ?object. FILTER(regex(str(?object),\'{VALUE}\',\'i\'))} } LIMIT 1',
			documentUri : 'SELECT DISTINCT * WHERE {<{URI}> ?property ?object} ORDER BY ?property',
			document : 'SELECT DISTINCT * WHERE {<{URI}> ?property ?object}',
			bnode : 'SELECT DISTINCT * WHERE {<{URI}> ?property ?object}',
			inverse : 'SELECT DISTINCT * WHERE {?object ?property <{URI}>.} LIMIT 100',
			inverseSameAs : 'SELECT DISTINCT * WHERE {{?object <http://www.w3.org/2002/07/owl#sameAs> <{URI}>} UNION {?object <http://www.w3.org/2004/02/skos/core#exactMatch> <{URI}>}}'
		},
		endpoint : 'http://localhost:8081/sparql',
		document : {
			className : 'standard',
			titleProperties : [ 'http://www.w3.org/2000/01/rdf-schema#label' ]
		},
		images : {
			properties : []
		},
		maps : {
			longs : [],
			lats : [],
			points : []
		},
		weblinks : {
			properties : []
		}
	}
});

if (!document.lodliveVars) {
	document.lodliveVars = {};
}

$.jStorage.set('boxTemplate', '<div class="boxWrapper" id="first"><div class="box sprite"></div></div>');
$.jStorage.set('relationsLimit', 25);
$.jStorage.set('doStats', $.jStorage.get('doStats', true));
$.jStorage.set('doInverse', $.jStorage.get('doAutoExpand', true));
$.jStorage.set('doAutoExpand', $.jStorage.get('doAutoExpand', true));
$.jStorage.set('doAutoSameas', $.jStorage.get('doAutoSameas', true));
$.jStorage.set('doCollectImages', $.jStorage.get('doCollectImages', true));
$.jStorage.set('doDrawMap', $.jStorage.get('doDrawMap', true));
$.jStorage.set('showInfoConsole', $.jStorage.get('showInfoConsole', true));

$.jStorage.set('endpoints', {
	all : 'output=json&format=application/json&timeout=0'
});
