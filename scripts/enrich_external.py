#!/usr/bin/env python3
"""
External / Rule-based enrichment for YAML files.

This script parses definition_en and raw_paragraphs to extract:
- symptoms (matching existing data/foundation/*.yaml)
- risk_factors (using regex/keyword matching)
- related_systems (based on symptom matches)

It only processes files where externally_enriched: false.

Usage:
    python scripts/enrich_external.py --data-dir data
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml


def load_yaml(file_path: Path) -> dict[str, Any] | None:
    """Load a YAML file."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)  # type: ignore[no-any-return]
    except Exception as e:
        print(f"Error loading {file_path}: {e}")
        return None


def save_yaml(file_path: Path, data: dict[str, Any]) -> None:
    """Save data to a YAML file."""
    with open(file_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def load_foundation_symptoms(foundation_dir: Path) -> dict[str, dict[str, Any]]:
    """Load all foundation symptom YAML files."""
    symptoms = {}
    
    if not foundation_dir.exists():
        return symptoms
    
    for yaml_file in foundation_dir.glob("*.yaml"):
        data = load_yaml(yaml_file)
        if data and "id" in data:
            symptoms[data["id"]] = data
    
    return symptoms


def extract_symptoms_from_text(
    text: str, 
    foundation_symptoms: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Extract symptom references from text by matching symptom titles."""
    found_symptoms = []
    text_lower = text.lower()
    
    for symptom_id, symptom_data in foundation_symptoms.items():
        title = symptom_data.get("title_en", "").lower()
        if not title:
            continue
        
        # Check if symptom title appears in text (word boundary match)
        # Use regex for more accurate matching
        pattern = r'\b' + re.escape(title) + r'\b'
        if re.search(pattern, text_lower):
            # Assign a default grade based on context clues
            grade = "OCCASIONAL"  # Default
            probability = 0.2
            
            # Check for strong indicators
            if any(word in text_lower for word in ["always", "characteristic", "hallmark", "typical"]):
                grade = "VERY_COMMON"
                probability = 0.8
            elif any(word in text_lower for word in ["common", "frequent", "usual"]):
                grade = "COMMON"
                probability = 0.5
            elif any(word in text_lower for word in ["rare", "uncommon", "infrequent"]):
                grade = "RARE"
                probability = 0.03
            
            found_symptoms.append({
                "id": symptom_id,
                "grade": grade,
                "probability": probability,
                "note": f"Extracted from definition/paragraphs",
            })
    
    return found_symptoms


# Predefined risk factor keywords and patterns
RISK_FACTOR_PATTERNS = [
    r"\b(age|aging|elderly|young|children|pediatric|infant)\b",
    r"\b(genetic|hereditary|familial|mutation|gene)\b",
    r"\b(environmental|exposure|contact|occupational)\b",
    r"\b(lifestyle|smoking|alcohol|diet|exercise|obesity|sedentary)\b",
    r"\b(immune|immunocompromised|immunosuppressed|HIV|AIDS)\b",
    r"\b(comorbidity|comorbid|chronic|diabetes|hypertension|cardiovascular)\b",
    r"\b(travel|endemic|epidemic|outbreak|geographic)\b",
    r"\b(gender|sex|male|female|pregnant|pregnancy)\b",
    r"\b(socioeconomic|poverty|malnutrition|sanitation)\b",
]

# Body system keywords for related_systems extraction
BODY_SYSTEMS = {
    "Respiratory system": ["respiratory", "lung", "pulmonary", "bronch", "pneum", "airway"],
    "Cardiovascular system": ["cardiac", "cardiovascular", "heart", "artery", "vein", "circulatory"],
    "Nervous system": ["neurological", "neural", "brain", "spinal", "nerve", "central nervous"],
    "Digestive system": ["gastrointestinal", "digestive", "stomach", "intestinal", "hepatic", "liver"],
    "Musculoskeletal system": ["musculoskeletal", "muscle", "bone", "joint", "skeletal", "arthr"],
    "Immune system": ["immune", "immunological", "autoimmune", "allergy", "hypersensitivity"],
    "Endocrine system": ["endocrine", "hormonal", "thyroid", "diabetes", "metabolic"],
    "Urinary system": ["urinary", "renal", "kidney", "bladder", "nephro"],
    "Reproductive system": ["reproductive", "genital", "ovarian", "testicular", "gynecological"],
    "Integumentary system": ["dermatological", "skin", "cutaneous", "epidermal"],
    "General": ["systemic", "general", "whole body", "constitutional"],
}


def extract_risk_factors(text: str) -> list[str]:
    """Extract risk factors from text using pattern matching."""
    risk_factors = []
    text_lower = text.lower()
    
    # Extract sentences that might contain risk factors
    sentences = re.split(r'[.!?]', text)
    
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        
        sentence_lower = sentence.lower()
        
        # Check if sentence contains risk factor indicators
        risk_indicators = [
            "risk factor", "associated with", "predisposing", "increased risk",
            "more common in", "prevalent in", "susceptible", "vulnerable"
        ]
        
        if any(indicator in sentence_lower for indicator in risk_indicators):
            # Clean and add as risk factor
            cleaned = sentence.strip()
            if cleaned and len(cleaned) > 10 and cleaned not in risk_factors:
                risk_factors.append(cleaned)
    
    # Also check for pattern matches
    for pattern in RISK_FACTOR_PATTERNS:
        matches = re.findall(pattern, text_lower)
        for match in matches:
            # Capitalize appropriately
            factor = match.capitalize()
            if factor not in risk_factors:
                risk_factors.append(factor)
    
    return risk_factors[:10]  # Limit to top 10


def extract_related_systems(
    text: str, 
    symptoms: list[dict[str, Any]], 
    foundation_symptoms: dict[str, dict[str, Any]]
) -> list[str]:
    """Extract related body systems based on text and symptoms."""
    systems = set()
    text_lower = text.lower()
    
    # Add systems from matched symptoms
    for symptom_ref in symptoms:
        symptom_id = symptom_ref.get("id")
        if symptom_id and symptom_id in foundation_symptoms:
            symptom_data = foundation_symptoms[symptom_id]
            for system in symptom_data.get("related_systems", []):
                systems.add(system)
    
    # Add systems from text patterns
    for system, keywords in BODY_SYSTEMS.items():
        for keyword in keywords:
            if keyword in text_lower:
                systems.add(system)
                break
    
    return sorted(list(systems))


def enrich_yaml_file(
    yaml_path: Path, 
    foundation_symptoms: dict[str, dict[str, Any]]
) -> bool:
    """Enrich a single YAML file with external data.
    
    Returns True if enrichment was performed, False if skipped.
    """
    data = load_yaml(yaml_path)
    if not data:
        return False
    
    # Check if already enriched
    if data.get("externally_enriched", False):
        print(f"  Skipping {yaml_path.name} (already enriched)")
        return False
    
    # Combine text sources for analysis
    text_sources = []
    
    if data.get("definition_en"):
        text_sources.append(data["definition_en"])
    
    if data.get("raw_paragraphs"):
        text_sources.extend(data["raw_paragraphs"])
    
    combined_text = " ".join(text_sources)
    
    if not combined_text.strip():
        print(f"  Skipping {yaml_path.name} (no text content)")
        return False
    
    # Extract symptoms
    symptoms = extract_symptoms_from_text(combined_text, foundation_symptoms)
    
    # Extract risk factors
    risk_factors = extract_risk_factors(combined_text)
    
    # Extract related systems
    related_systems = extract_related_systems(combined_text, symptoms, foundation_symptoms)
    
    # Update YAML data
    data["symptoms"] = symptoms
    data["risk_factors"] = risk_factors
    data["externally_enriched"] = True
    
    # Save updated YAML
    save_yaml(yaml_path, data)
    
    print(f"  Enriched {yaml_path.name}: {len(symptoms)} symptoms, {len(risk_factors)} risk factors")
    return True


def main(data_dir: Path) -> int:
    """Main entry point for external enrichment."""
    mms_dir = data_dir / "mms"
    foundation_dir = data_dir / "foundation"
    
    if not mms_dir.exists():
        print(f"MMS directory not found: {mms_dir}")
        return 1
    
    # Load foundation symptoms
    foundation_symptoms = load_foundation_symptoms(foundation_dir)
    print(f"Loaded {len(foundation_symptoms)} foundation symptoms")
    
    if not foundation_symptoms:
        print("Warning: No foundation symptoms loaded. Symptom extraction will be limited.")
    
    # Find all YAML files that need enrichment
    yaml_files = list(mms_dir.glob("*.yaml"))
    
    if not yaml_files:
        print("No YAML files found in mms directory")
        return 0
    
    enriched_count = 0
    skipped_count = 0
    
    for yaml_file in yaml_files:
        if enrich_yaml_file(yaml_file, foundation_symptoms):
            enriched_count += 1
        else:
            skipped_count += 1
    
    print(f"\nExternal enrichment complete:")
    print(f"  Enriched: {enriched_count} files")
    print(f"  Skipped: {skipped_count} files")
    
    return 0


def cli() -> int:
    """CLI entry point with argument parsing."""
    parser = argparse.ArgumentParser(description="External/rule-based YAML enrichment")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).parent.parent / "data",
        help="Data directory (default: ../data)",
    )
    
    args = parser.parse_args()
    return main(args.data_dir)


if __name__ == "__main__":
    sys.exit(cli())
