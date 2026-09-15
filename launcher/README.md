# launcher

Tools for the ROM: Golden Age launcher.

## appsettings_crypto.py

Decrypt / encrypt the launcher's `appsettings.json` (`ENC: <base64>` blob).

Scheme (reversed from `Launcher.Settings`, AES-256-CBC):

```
key    = SHA256(p1 + p4 + p2 + p3)   # 32 bytes
iv     = key[:16]
cipher = AES-256-CBC, PKCS7
file   = "ENC: " + base64(cipher(utf-8 plaintext))
```

The `pN` constants are hard-coded in the launcher. Key + IV are fixed, so
encryption is deterministic: re-encrypting the decrypted config reproduces the
shipped ciphertext byte-for-byte.

### Usage

```bash
# decrypt to stdout
python3 appsettings_crypto.py decrypt appsettings.json

# decrypt to a file
python3 appsettings_crypto.py decrypt appsettings.json -o appsettings.decrypted.json

# encrypt an edited config back into the ENC: form the launcher reads
python3 appsettings_crypto.py encrypt appsettings.decrypted.json -o appsettings.json
```

`-` means stdin/stdout. Plaintext input without the `ENC: ` prefix passes
through `decrypt` unchanged (matching the launcher's own behaviour).

### Requirements

- Python 3.8+
- `pycryptodome` (`pip install pycryptodome`)
