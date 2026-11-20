"""Stable contract hashing shared by trusted library operator commands."""
import hashlib,json
def contract_hash(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
