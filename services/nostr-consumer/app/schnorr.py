import secp256k1


def verify_schnorr(pubkey_hex: str, sig_hex: str, msg_id_hex: str) -> bool:
    try:
        pub_bytes = bytes.fromhex(pubkey_hex)
        sig_bytes = bytes.fromhex(sig_hex)
        msg_bytes = bytes.fromhex(msg_id_hex)
        try:
            pk = secp256k1.PublicKey(pub_bytes, raw=True)
        except Exception:
            pk = secp256k1.PublicKey(b"\x02" + pub_bytes, raw=True)
        return pk.schnorr_verify(msg_bytes, sig_bytes, None, raw=True)
    except Exception as e:
        print(f"❌ 驗證失敗: {e}")
        return False


def sign_schnorr(priv_key_hex: str, msg_id_hex: str) -> str:
    priv_bytes = bytes.fromhex(priv_key_hex)
    pk = secp256k1.PrivateKey(priv_bytes, raw=True)
    msg_bytes = bytes.fromhex(msg_id_hex)
    sig_bytes = pk.schnorr_sign(msg_bytes, None, raw=True)
    return sig_bytes.hex()
