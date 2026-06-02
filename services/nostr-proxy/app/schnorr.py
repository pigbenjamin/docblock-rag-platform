import hashlib
import secp256k1


def sign_schnorr(priv_key_hex: str, msg_id_hex: str) -> str:
    priv_bytes = bytes.fromhex(priv_key_hex)
    pk = secp256k1.PrivateKey(priv_bytes, raw=True)
    msg_bytes = bytes.fromhex(msg_id_hex)
    sig_bytes = pk.schnorr_sign(msg_bytes, None, raw=True)
    return sig_bytes.hex()
