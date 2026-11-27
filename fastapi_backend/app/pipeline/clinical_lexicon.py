"""Real clinical words the corpus corrector must never rewrite.

Correction exists for ASR *mis-renderings* ("para set a mole"), not for
swapping one real term for another. A deployment's corpus is a handful of
terms; it cannot be expected to list every look-alike of each, so when the
corpus says "hyperglycaemia" a student who correctly says "hypoglycaemia"
(difflib ratio 0.889, over the 0.84 threshold) would otherwise be rewritten
into the opposite diagnosis.

This is a small bundled allow-list, consulted through the same protected-term
check as the corpus's own spellings (``transcript_correction._ProtectedTerms``).
It is deliberately a list of *real, easily confused* terms — both members of
every confusable pair, in UK and US spelling — and not a dictionary: a word
listed here is never corrected, so a misspelling must never be added to it.
Extend it as one set of related terms at a time.
"""
from __future__ import annotations

# Pairs/groups that differ by one or two letters and mean different things.
_CONFUSABLE_GROUPS: tuple[tuple[str, ...], ...] = (
    ("hypoglycaemia", "hyperglycaemia", "hypoglycemia", "hyperglycemia"),
    ("hypotension", "hypertension"),
    ("hypothermia", "hyperthermia"),
    ("hypokalaemia", "hyperkalaemia", "hypokalemia", "hyperkalemia"),
    ("hyponatraemia", "hypernatraemia", "hyponatremia", "hypernatremia"),
    ("hypocalcaemia", "hypercalcaemia", "hypocalcemia", "hypercalcemia"),
    ("hypothyroidism", "hyperthyroidism"),
    ("hypoxia", "hypoxaemia", "hypoxemia", "hyperoxia"),
    ("hypoventilation", "hyperventilation"),
    ("hypotonia", "hypertonia"),
    ("dysphasia", "dysphagia", "dysphonia", "dyspepsia", "dyspnoea", "dyspnea", "dysuria", "dyspraxia"),
    ("aphasia", "aphagia", "aphonia", "apraxia"),
    ("prednisone", "prednisolone", "methylprednisolone"),
    ("hydroxyzine", "hydralazine"),
    ("clonidine", "clonazepam", "clozapine"),
    ("losartan", "valsartan", "irbesartan", "candesartan"),
    ("simvastatin", "atorvastatin", "rosuvastatin", "pravastatin"),
    ("metformin", "metronidazole", "metoprolol", "methotrexate"),
    ("amlodipine", "amiodarone", "amitriptyline"),
    ("lamivudine", "lamotrigine", "levothyroxine", "levetiracetam"),
    ("glibenclamide", "gliclazide", "glimepiride", "glipizide"),
    ("ranitidine", "rifampicin", "ritonavir"),
    ("salbutamol", "salmeterol", "terbutaline"),
    ("fluoxetine", "paroxetine", "sertraline", "citalopram", "escitalopram"),
    ("diazepam", "lorazepam", "temazepam", "midazolam"),
    ("ischaemia", "ischemia", "anaemia", "anemia", "septicaemia", "septicemia"),
    ("ileum", "ilium"),
    ("ureter", "urethra"),
    ("diarrhoea", "diarrhea", "dysentery"),
    ("pleural", "peritoneal", "pericardial"),
    ("peritonitis", "pericarditis", "pleuritis", "pancreatitis", "appendicitis"),
    ("gastritis", "gastroenteritis", "enteritis", "colitis"),
    ("cystitis", "pyelonephritis", "nephritis", "urethritis"),
    ("bronchitis", "bronchiolitis", "tonsillitis", "pharyngitis", "laryngitis"),
    ("osteoporosis", "osteoarthritis", "osteomyelitis", "osteopenia"),
    ("tachycardia", "bradycardia", "tachypnoea", "bradypnoea", "tachypnea", "bradypnea"),
    ("melanoma", "lymphoma", "sarcoma", "carcinoma", "glaucoma", "cataract"),
    ("epilepsy", "narcolepsy", "catalepsy"),
    ("diplopia", "diplegia", "dysplasia", "hemiplegia", "paraplegia", "quadriplegia"),
)


CLINICAL_LEXICON: frozenset[str] = frozenset(word for group in _CONFUSABLE_GROUPS for word in group)
