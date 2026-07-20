# Data Retention and Privacy

## Data classes

| Data | Official retention | Public? |
|---|---:|---:|
| Signed sanitized trial bundle | Indefinite | Yes |
| Raw sealed trajectory/logs | 180 days | No |
| Hidden task/grader material | Until retirement plus two releases | No |
| Provider request identifiers | 180 days | Sanitized/hashed only |
| Cost/token/resource telemetry | Indefinite in aggregate | Yes |
| Credentials and capability tokens | Never persisted | No |
| Security incident evidence | Per incident/legal requirements | No |

## Collection minimization

Official runs collect only evidence needed to reproduce execution, verify budgets, grade outputs,
investigate failures, and audit score integrity.

Public bundles must not include:

- provider secrets;
- authorization headers;
- private model reasoning payloads;
- private repository content outside the task contract;
- personal email, chat, or unrelated user files;
- hidden grader source before retirement.

## Deletion

Raw sealed evidence is deleted after 180 days unless:

- an active dispute exists;
- a security incident requires preservation;
- applicable law requires longer retention.

Deletion never removes the public signed result or invalidation record.

## Model-provider disclosure

Every benchmark release documents provider retention settings, whether prompts/responses may be used
for training, and which request metadata is retained by the benchmark.
