import bcrypt

# A palavra-passe plana
senha_plana = "123456"

# O bcrypt exige que a palavra-passe seja convertida para "bytes"
bytes_senha = senha_plana.encode('utf-8')

# Gera o salt (uma camada extra de segurança) e faz o hash
salt = bcrypt.gensalt()
senha_hash = bcrypt.hashpw(bytes_senha, salt)

print("Copie o Hash abaixo:")
# Converte de volta para texto (string) para poder guardar no SQL Server
print(senha_hash.decode('utf-8'))