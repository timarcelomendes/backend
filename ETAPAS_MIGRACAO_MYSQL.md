# Etapas da Migracao para MySQL

Objetivo:
Executar a troca do backend de SQL Server para MySQL de forma controlada, validando cada fase antes de avancar.

Como usar:
- Execute uma etapa por vez.
- So avance para a proxima quando todos os criterios de validacao da etapa atual estiverem OK.
- Marque o status de cada etapa.

Status geral:
- [x] Etapa 0 - Baseline e backup
- [x] Etapa 1 - Ambiente MySQL pronto
- [x] Etapa 2 - Configuracao de conexao no backend
- [x] Etapa 3 - Conversao inicial de queries criticas
- [ ] Etapa 4 - Ajuste das queries de relatorios e filtros
- [ ] Etapa 5 - Testes funcionais por modulo
- [ ] Etapa 6 - Validacao final de consistencia
- [ ] Etapa 7 - Go-live

---

## Etapa 0 - Baseline e backup

Status: [x]

Objetivo:
Congelar referencia atual antes das mudancas.

Checklist:
- [x] Confirmar que o MySQL possui todas as tabelas e contagens equivalentes.
- [x] Exportar backup do MySQL atual.
- [x] Registrar hash/versao atual do codigo para rollback.

Validacao esperada:
- [x] Existe backup restauravel do MySQL.
- [x] Existe ponto de rollback de codigo.

---

## Etapa 1 - Ambiente MySQL pronto

Status: [x]

Objetivo:
Garantir que o banco de destino esta estavel e acessivel.

Checklist:
- [x] Subir servico MySQL.
- [x] Confirmar usuario, senha, schema e collation.
- [x] Confirmar acesso a partir do container do backend.

Validacao esperada:
- [x] Conexao MySQL funciona de dentro do backend.
- [x] As 16 tabelas nps_ existem no schema nps.

---

## Etapa 2 - Configuracao de conexao no backend

Status: [x]

Objetivo:
Trocar a conexao padrao da API para MySQL.

Arquivos alvo:
- database.py
- .env
- requirements.txt
- Dockerfile

Checklist:
- [x] Trocar engine SQLAlchemy para mysql+pymysql em database.py.
- [x] Ler variaveis MYSQL_HOST, MYSQL_PORT, MYSQL_DATABASE, MYSQL_USER, MYSQL_PASSWORD.
- [x] Manter variaveis MSSQL apenas para scripts de migracao/validacao (nao para runtime da API).
- [x] Garantir pymysql em requirements.
- [x] Remover dependencia de runtime em drivers SQL Server no Dockerfile, se nao forem mais necessarios.

Validacao esperada:
- [x] Endpoint de health sobe com backend conectado ao MySQL.
- [x] Log do backend nao mostra tentativa de conexao SQL Server em runtime.

---

## Etapa 3 - Conversao inicial de queries criticas

Status: [x]

Objetivo:
Converter primeiro as queries de autenticacao, configuracao e fluxos principais.

Arquivos alvo (prioridade alta):
- main.py
- services/respostas_svc.py
- services/clientes_svc.py
- services/email_svc.py
- routers/chat.py

Mapa de conversao SQL:
- dbo.tabela -> tabela
- SELECT TOP N -> LIMIT N
- GETDATE() -> NOW()
- CAST(GETDATE() AS DATE) -> CURDATE()
- DATEADD(day, X, data) -> DATE_ADD(data, INTERVAL X DAY)
- DATEDIFF(day, a, b) -> TIMESTAMPDIFF(DAY, a, b)
- ISNULL(a, b) -> IFNULL(a, b)
- LEN(texto) -> CHAR_LENGTH(texto)
- CAST(... AS NVARCHAR(MAX)) -> CAST(... AS CHAR)
- IF EXISTS + UPDATE/INSERT -> SELECT previo + UPDATE/INSERT ou INSERT ... ON DUPLICATE KEY UPDATE

Checklist:
- [x] Remover prefixo dbo de todas as tabelas usadas nesses modulos.
- [x] Converter TOP/GETDATE/DATEADD/DATEDIFF/ISNULL/LEN.
- [x] Substituir blocos IF EXISTS/BEGIN/END por logica compativel com MySQL.

Validacao esperada:
- [x] Login funciona.
- [x] Leitura e escrita de configuracoes funciona.
- [x] Endpoints de respostas/clientes/chat retornam sem erro SQL.

---

## Etapa 4 - Ajuste das queries de relatorios e filtros

Status: [ ]

Objetivo:
Corrigir consultas mais densas (dashboard, funis, segmentacoes, analiticos).

Checklist:
- [ ] Revisar consultas com subqueries de TOP 1 e trocar por ORDER BY ... LIMIT 1.
- [ ] Revisar consultas de periodos (3, 6, 12 meses) para equivalencia com MySQL.
- [ ] Revisar filtros de texto e null-safe comparators.

Validacao esperada:
- [ ] Numeros de dashboard batem com referencia do SQL Server para amostras conhecidas.
- [ ] Filtros por periodo, companhia e empresa retornam dados esperados.

---

## Etapa 5 - Testes funcionais por modulo

Status: [ ]

Objetivo:
Validar comportamento completo da API com MySQL.

Checklist:
- [ ] Autenticacao: login, refresh de sessao, alterar senha, reset de senha.
- [ ] Cadastros: clientes, empresas, perfis, segmentos, cargos, gestores.
- [ ] Respostas: listagem, filtros, edicao, exclusao logica.
- [ ] Acoes: criacao, atualizacao de status, kanban.
- [ ] Disparos/email: fila, envio, status_envio, erros.
- [ ] Configuracoes: leitura e persistencia.

Validacao esperada:
- [ ] Sem erro 500 por SQL em rotas principais.
- [ ] Sem regressao funcional critica.

---

## Etapa 6 - Validacao final de consistencia

Status: [ ]

Objetivo:
Garantir que nao houve perda de dados nem distorcao relevante.

Checklist:
- [ ] Rodar comparacao SQL Server x MySQL (script de comparacao).
- [ ] Confirmar contagem por tabela.
- [ ] Confirmar divergencias residuais aceitaveis (ex.: arredondamento de 1 segundo).
- [ ] Confirmar campos sensiveis sem divergencia (ids, emails, notas, status, textos).

Criterio de aprovacao:
- [ ] Zero perda de linhas.
- [ ] Divergencias apenas nas tolerancias aprovadas.

---

## Etapa 7 - Go-live

Status: [ ]

Objetivo:
Colocar MySQL como banco oficial de producao da API.

Checklist:
- [ ] Definir variaveis de ambiente finais para MySQL.
- [ ] Reiniciar backend em modo producao.
- [ ] Monitorar logs e metricas por 24h.
- [ ] Ter plano de rollback documentado e testado.

Validacao esperada:
- [ ] Sistema estavel em producao.
- [ ] Sem erros SQL recorrentes.

---

## Registro de execucao

Preencha durante a migracao:

- Responsavel:
- Data inicio:
- Data fim:
- Etapa atual:
- Bloqueios encontrados:
- Decisoes tomadas:
- Resultado final:
