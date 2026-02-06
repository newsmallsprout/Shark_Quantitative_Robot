from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
import os

def generate_keys(output_dir="license"):
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    # Save Private Key (Keep this SAFE!)
    with open(os.path.join(output_dir, "private.pem"), "wb") as f:
        f.write(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ))

    # Save Public Key (Distribute with Bot)
    public_key = private_key.public_key()
    with open(os.path.join(output_dir, "public.pem"), "wb") as f:
        f.write(public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        ))
    
    print(f"Keys generated in {output_dir}")

if __name__ == "__main__":
    generate_keys()
