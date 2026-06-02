# ITIAI ICD-11 Knowledge Base

A structured knowledge base for ICD-11 disease classifications with symptom foundations and AI enrichment capabilities.

## Structure

```
antigravity/
├── data/
│   ├── mms/           # Disease definitions (one per ICD-11 code)
│   ├── foundation/    # Symptom definitions
│   ├── research/      # Research notes and references
│   └── generated/     # Generated artifacts (FAISS indices, etc.)
├── schemas/           # JSON Schema definitions
├── scripts/           # Utility scripts
├── tests/             # Test suite
└── .github/workflows/ # CI/CD workflows
```

## Installation

```bash
pip install -e ".[dev]"
```

## Usage

### Validate Data

```bash
python scripts/validate.py
```

This validates:
- All disease YAML files against the disease schema
- All symptom YAML files against the symptom schema
- Cross-references between diseases and symptoms
- Grade/probability consistency for symptoms

### Run Tests

```bash
pytest
```

### Linting and Type Checking

```bash
ruff check .
mypy scripts
```

## Data Contracts

### Disease Schema (`data/mms/{code}.yaml`)

Required fields:
- `entity_uri`: ICD-11 entity URI
- `code`: ICD-11 code
- `title_en`: English title
- `definition_en`: English definition
- `parent_code`: Parent ICD-11 code (or null)
- `children_codes`: Array of child codes
- `pathophysiology_en`: Pathophysiology description
- `symptoms`: Array of symptom references
- `differential_diagnosis`: Array of differential diagnoses
- `risk_factors`: Array of risk factors
- `drugs`: Array of associated drugs
- `vector_text_en`: Text for vector embeddings
- `stats`: Statistics object
- `ai_enriched`: Boolean flag for AI enrichment
- `last_updated`: ISO date string

### Symptom Schema (`data/foundation/{id}.yaml`)

Required fields:
- `id`: Unique symptom identifier
- `title_en`: English title
- `definition_en`: English definition
- `related_systems`: Array of related body systems

### Symptom Grades

| Grade | Probability Range |
|-------|------------------|
| ALWAYS | 1.0 |
| VERY_COMMON | 0.7 - 0.99 |
| COMMON | 0.3 - 0.69 |
| OCCASIONAL | 0.05 - 0.29 |
| RARE | 0.001 - 0.049 |
| NEVER | 0.0 |

## License

MIT
