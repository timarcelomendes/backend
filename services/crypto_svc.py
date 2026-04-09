import os
from cryptography.fernet import Fernet

def get_cipher():
    """Obtém a chave mestra do ambiente."""
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        print("⚠️ AVISO DE SEGURANÇA: ENCRYPTION_KEY não definida no .env.")
        return None
    try:
        return Fernet(key.encode())
    except Exception as e:
        print(f"⚠️ Erro na chave de criptografia: {e}")
        return None

def encrypt_data(data: str) -> str:
    """Recebe um texto limpo e devolve uma string criptografada."""
    if not data: return data
    cipher = get_cipher()
    if not cipher: return data # Fallback se não houver chave
    return cipher.encrypt(data.encode()).decode()

def decrypt_data(token: str) -> str:
    """Recebe a string criptografada e devolve o texto limpo."""
    if not token: return token
    cipher = get_cipher()
    if not cipher: return token
    try:
        return cipher.decrypt(token.encode()).decode()
    except Exception:
        # 🎯 INTELIGÊNCIA: Se der erro ao destrancar, significa que a senha na base de dados 
        # ainda está em texto plano (sem criptografia). Ele retorna o texto original 
        # para que a sua plataforma não saia do ar durante a transição!
        return token