# Implementation order
1. models.py — add mutable_metadata, transfer_fee_enable to Candidate + to_dict
2. deterministic_filter.py — new return type (hard_fail, reason, soft_flags)
3. llm_scorer.py — add soft_flags param to score_candidate + _build_scoring_prompt
4. data_ingestion.py — add _fetch_birdeye_security, enrich candidates
5. main.py — update tick loop to use new filter shape
6. tests/test_filter_split.py — new test file
