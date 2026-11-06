# knowledge/

Governed knowledge the broker reads and returns as references. It is guidance for reasoning about
network context. It is never an instruction to change a network, and nothing in the broker executes,
schedules or approves an action.

## Layout

```
knowledge/
  README.md
  action-catalogue.yaml
  bundles/<identifier>/<version>.md
```

A bundle is one Markdown file with YAML front matter. The directory name is the identifier and the
file name (without `.md`) is the version, and both must equal the `identifier` and `version` in the
front matter. The version is quoted (`"1.0"`) and the highest version of an identifier is the only
one the broker considers. The front matter holds `identifier`, `version`, `status`, `title`, `scope`
(`entities`, `purposes`, and optionally `ontology_classes`), `source_references` and `linked_actions`.
Files are UTF-8 with LF line endings and no byte order mark (`.gitattributes` enforces LF), because
the content hash covers the exact bytes.

A source reference is `repo:<tracked path>`, `metric:<pinned metric short name>` or
`ontology:<class in the tracked TBox>`.

## Statuses

| Status | What the broker does with it |
|---|---|
| `draft` | Returned, with its status shown. Not an approval. |
| `reviewed` | Returned, with its status shown. Not an approval. |
| `approved` | Returned, with its status shown. |
| `withdrawn` | Never returned. A withdrawn latest version also hides older versions of the identifier. |

The broker never sums these into an approval of its own.

## Approval

Approval is the owner changing `status` in a reviewed commit. The broker only reads the field and
never writes it. Every bundle here is `draft`. A change to a file takes effect when the broker is
restarted.

## The action catalogue

`action-catalogue.yaml` lists example action templates with seven fields: `template_id`, `version`,
`scope_pattern`, `preconditions`, `expected_outcome`, `recovery` and `approved`. A bundle may name a
template in `linked_actions`. The broker returns an approved template as a reference only. The
catalogue here holds one read-only example marked approved and one example marked not approved.

## Rules for new text

- Write it in this project's own words, from the tracked design notes and the pinned metric names.
- Do not copy licensed material into any file here. The licensed documents stay outside the
  repository.
- Say "goal" for one metric with its limit.
- A bundle body describes what to read and how to interpret it. It does not tell anyone to change
  the network.
