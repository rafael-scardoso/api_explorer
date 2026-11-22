# API Explorer

Interface gráfica (Tkinter) para testar APIs hidrometeorológicas. Uma aba por API.

## Como rodar
    Atualização: Inserido atalho para rodar direto.
    pip install -r requirements.txt
    python app.py
No Linux, se faltar o Tkinter: `sudo apt install python3-tk`.

## Abas
| Aba | Cadastro | Requisições |
|---|---|---|
| Defesa Civil RS | não | tags_data (todas as estações), historic, livre |
| Open-Meteo | não (chave opcional para plano comercial) | forecast, archive, flood, geocoding, elevação, livre |
| OpenZenith | não | elevação, health, carregar rotas (OpenAPI), livre |
| ANA HidroWebService | identificador e senha para dados; OpenAPI público | autenticação, OpenAPI, inventário, série adotada, série detalhada, livre |
| REINDESC | não documentado | consulta ao portal público e requisição livre |

## Consultas prontas

1. Abra a aba da API e clique no botão da consulta.
2. Revise os campos preenchidos e complete os que têm asterisco.
3. Clique em **Executar** ou em **Ver requisição (curl)** para conferir o comando.

### Listagens e histórico

- **Defesa Civil RS · Listar estações** mostra todas as estações entregues pela rede com código,
  nome, cidade, rio, latitude e longitude. O município é identificado com a malha municipal do IBGE.
  Campos que não puderem ser identificados ficam em branco.
- **Open-Meteo · Histórico** aceita 10 anos ou mais em uma consulta. O aplicativo divide o período
  em blocos consecutivos de até 90 dias, mostra o progresso e reúne os dados nas tabelas horária e diária.
  Use **Anterior** e **Próxima** para navegar pelas tabelas longas. **Exportar CSV** salva todas as
  linhas da tabela selecionada que correspondem ao filtro, incluindo as demais páginas. Para baixar
  o período completo, deixe o filtro vazio. O ERA5 tem dados desde 1940 e atraso de cerca de cinco dias.
- **ANA · Listar estações** mostra município, UF, latitude, longitude, código e nome da bacia
  das estações do inventário. A consulta usa as credenciais da ANA e o filtro selecionado.
- **REINDESC · Consulta manual** explica os formatos aceitos no formulário HTTP. Os nomes e
  valores dos parâmetros dependem da rota informada.

Em **Mostrar opções avançadas** ficam parâmetros como variáveis do Open-Meteo,
tipo de data da ANA e endereço da especificação OpenAPI. **Consulta manual**
abre o formulário com método, URL, parâmetros, cabeçalhos e corpo.

Na ANA, o inventário já abre com UF RS. Para buscar uma estação ou bacia sem
filtro estadual, deixe a UF vazia. As séries exigem o código da estação e trazem
o intervalo preenchido. A autenticação usa os dados informados no topo da aba.
No REINDESC, **Consultar portal** abre uma consulta à página pública.

## Recursos
- Resultado em tabela (ordenável, filtro de texto, duplo clique mostra a linha como campo/valor),
  JSON bruto e a requisição equivalente em curl (senha, token e apikey mascarados).
- Exportar CSV (separador `;`, UTF-8 com BOM para Excel) e JSON.
- "Verificar SSL", timeout e User-Agent configuráveis no topo.
- ANA: o token é obtido automaticamente em consultas da mesma origem e renovado uma vez em caso de 401/403.
  A resposta de autenticação exibida e exportada tem os campos de token mascarados.
- A URL final, os cabeçalhos sensíveis exibidos e os parâmetros de autenticação na URL do cURL são mascarados.

## Observações
- O inventário da ANA exige UF, código da estação ou código da bacia; o formulário já traz a UF RS. As séries exigem código da estação, tipo de filtro e intervalo.
- OpenZenith usa `openzenith.org` por padrão; o endereço anterior continua disponível no seletor.
- Defesa Civil RS: limite de 120 req/min por IP; dados atualizam a cada 5 min.
- Open-Meteo gratuito: uso não comercial (600/min, 5.000/h, <10.000/dia), dados CC BY 4.0.
- ANA: token vale ~60 min (o manual cita 15 min no exemplo); evite autenticar à toa (risco de bloqueio de IP). HTTP 429: aguarde ~90 s.
- O PDF incluído acompanha a edição original. Para os endereços padrão e os formulários revisados, vale este README.

## Testes
`python tests/smoke_test.py` usa Tkinter simulado e um servidor HTTP local (sem rede externa).
`python -m unittest discover -s tests` cobre as regressões com respostas controladas.
`python tests/live_test.py` usa os formulários Tkinter e o cliente HTTP do aplicativo contra os serviços públicos reais.
Requer rede, DNS e Tkinter. O teste público da ANA lê apenas o OpenAPI. O teste do REINDESC verifica o portal.
