# Resumo para continuidade

Última atualização: 2026-09-04.

Este documento permite continuar o trabalho sem acesso à conversa que originou o
projeto. Para o racional técnico e de segurança completo, leia também
[`HANDOFF-sdk-team.md`](HANDOFF-sdk-team.md).

## Estado atual

- Repositório oficial: `mercadopago/commerce-agents-checkout` (privado).
- PR de importação: https://github.com/mercadopago/commerce-agents-checkout/pull/1
- Branch: `import-checkout-adapter`.
- Commit de importação: `1c5c95b`, válido e assinado pelo GitHub.
- Conta usada para operar o repositório: `gdeandradero` (sem `_meli`).
- Origem do protótipo: https://github.com/gforgab/mercadopago-commerce-agents,
  commit `b384600`.
- O `main` da organização foi preservado. Não houve force-push.

O PR está aberto e mergeável, mas a organização exige aprovação de outro code owner e
aprovação de alguém diferente de quem fez o último push.

## O que foi construído

Um pacote Python independente que implementa `checkout_handoff` para a demo
`anthropics/commerce-agents` criando uma preferência de Checkout Pro com o SDK oficial
do Mercado Pago.

O pacote não depende de `shopping-agent-core` em runtime. Esse projeto da Anthropic não
é publicado no PyPI de propósito; a integração retorna um `CheckoutHandoff`
estruturalmente compatível e mantém um teste de contrato contra o código upstream real.

Uso pretendido:

```python
import os

import mercadopago

from mercadopago_commerce_agents import MercadoPagoCheckout

sdk = mercadopago.SDK(os.environ["MERCADOPAGO_ACCESS_TOKEN"])
checkout = MercadoPagoCheckout(sdk=sdk, catalog=backend)
```

## Decisões já tomadas

- O adaptador fica fora de `sdk-python`, mas depende de `mercadopago>=3.5.0`.
- O construtor exige somente `sdk` e `catalog`; `reference_store` é opcional.
- Não existe `from_env`: a aplicação carrega o token e configura o SDK, que é a única
  fonte de credenciais, timeout, retries e headers.
- `currency_id` não é enviado. O catálogo deve fornecer preços na moeda da conta e o
  comportamento real da API ainda precisa ser validado.
- A URL de webhook não é configurada por checkout; ela fica na aplicação do Mercado
  Pago. A validação de `x-signature` continua sendo responsabilidade do backend.
- O TTL da preferência é fixo em 24 horas.
- O label do botão fica a cargo do host de `commerce-agents`.
- Hosts válidos de Checkout Pro são uma allowlist interna, não configuração pública.
- As opções do SDK são copiadas por request para adicionar idempotência sem mutar a
  instância compartilhada.
- A mesma sessão e o mesmo carrinho geram chave de idempotência e
  `external_reference` estáveis.

## Decisões de segurança preservadas

- Nunca usar o preço vindo do carrinho/modelo. Cada item é reprecificado no catálogo
  confiável do seller antes da criação da preferência.
- Nunca usar `session_id` diretamente como `external_reference`.
- Sem catálogo ou com produto inválido/sem estoque, a preferência não é criada.
- O token não é logado nem aparece no `repr` do adaptador.
- Erros da API não registram payload, títulos, preços ou referências sensíveis.
- O `init_point` retornado é validado contra hosts HTTPS do Mercado Pago.
- Não adicionar `shopping-agent-core` como dependência do PyPI.

## Validação executada

- 26 testes passaram em Python 3.12, incluindo o contrato real com
  `commerce-agents@fd4d59224ab96b43c6dc6888207c67b3bd5a24cf`.
- A CI está configurada para Python 3.11 e 3.12.
- `pylint`: 10.00/10.
- `isort`: limpo.
- Build de wheel e sdist: aprovado.
- `twine check`: aprovado.
- Instalação do wheel em ambiente limpo, sem Anthropic: aprovada.
- SDK validado localmente: `mercadopago==3.5.0`.
- Bandit: nenhum finding.
- Auditoria OSV das dependências resolvidas: nenhuma vulnerabilidade conhecida.

Nenhuma chamada real foi feita à API do Mercado Pago.

## Bloqueio de CI

O GitHub Actions está desabilitado para este repositório por política da organização:

```text
actions/permissions -> enabled: false
```

Por isso, os workflows importados não rodam hoje. O fato e os resultados locais também
estão registrados em um comentário no PR. Um administrador da organização precisa
habilitar Actions para este repositório ou indicar a este projeto a CI oficial que deve
substituí-lo.

## Próximos passos, em ordem

1. Revisar o PR e pedir a aprovação obrigatória de outro code owner.
2. Resolver a CI desabilitada e executar os jobs em Python 3.11 e 3.12.
3. Obter um token `TEST-` e criar uma preferência real. Confirmar principalmente:
   - aceitação de `items[]` sem `currency_id`;
   - formato de `expiration_date_to`;
   - aceitação de `items[].id`;
   - retorno de um `init_point` compatível com a allowlist.
4. Repetir o teste com contas de dois sites/moedas, se possível. A biblioteca não faz
   conversão de moeda.
5. Confirmar os três nomes antes do primeiro release:
   - repositório: `commerce-agents-checkout`;
   - distribuição provisória no PyPI: `mercadopago-commerce-agents`;
   - import Python atual: `mercadopago_commerce_agents`.
6. Criar o projeto no PyPI sob uma organização do Mercado Pago, nunca em conta pessoal.
7. Decidir se o release usará Trusted Publishing/OIDC, como está em `cd.yml`, e
   configurar o environment `pypi` no GitHub/PyPI.
8. Confirmar Apache-2.0 ou alinhar a licença com o MIT de `sdk-python`.
9. Fazer merge e publicar a versão `0.1.0` somente depois do teste real da API.

## Como retomar localmente

```bash
gh auth switch --hostname github.com --user gdeandradero
gh repo clone mercadopago/commerce-agents-checkout
cd commerce-agents-checkout
gh pr checkout 1

python -m venv .venv
.venv/bin/pip install -e . pylint isort build twine

git clone https://github.com/anthropics/commerce-agents.git
.venv/bin/pip install ./commerce-agents/commerce-common \
  ./commerce-agents/shopping-agent/core

.venv/bin/python -m unittest discover -s tests
.venv/bin/pylint --max-line-length=100 src/mercadopago_commerce_agents
.venv/bin/isort --check-only --diff src tests
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
```

## Critério mínimo para considerar pronto

- PR aprovado e mergeado.
- CI oficial habilitada e verde.
- Preferência aceita por uma conta de teste real sem `currency_id` explícito.
- Nome definitivo e ownership do PyPI confirmados.
- Trusted Publishing ou mecanismo oficial de publicação configurado.

Credenciais de teste, acesso ao PyPI e aprovação do PR não estão neste repositório e
precisam ser fornecidos pelo time responsável.
