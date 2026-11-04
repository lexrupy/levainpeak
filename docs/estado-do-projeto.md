# LevainPeak — estado do projeto

Nota para retomar numa conversa nova. Atualizada em 2026-10-31, com 81 testes passando. Complementa o `AGENTS.md` (regras do repositório) e o `README.md`.

## O que o app faz

Monitora um levain por uma câmera RTSP fixa. Mostra o vídeo, o pote detectado, o volume relativo,
a fase e a temperatura lida do visor de um termômetro. Grava uma linha de CSV a cada 10 s por padrão.

| Módulo (`src/levainpeak/`) | Papel |
|---|---|
| `stream.py` | Captura RTSP. |
| `analysis.py` | Thread de análise (`ContainerAnalyzer`): mede o pote, estabiliza, lê o termômetro, escolhe o perfil. |
| `vision.py` | `measure_container`, `column_scan` (superfície por claridade, com reserva por bordas), perfil de largura e volume. |
| `temperature.py` | OCR do visor de 7 segmentos (dia e infravermelho). |
| `profiles.py`, `imaging.py` | Perfil de captura `dia` / `infravermelho`, escolhido pela cor da imagem. |
| `filters.py` | `OutlierGate`, `despike`, `valid_after_recovery`. |
| `sessions.py` | `SessionLog` (CSV e JSON da sessão), `PhaseTracker`. |
| `forecast.py` | Previsão do dobro, com a temperatura como contexto. |
| `graph_phases.py` | Fases do gráfico: Aquecimento, Crescimento, Pico, Decaimento. O estado ao vivo usa Aquecendo, Crescendo, Estabilizando, Em pico,
  Queda em confirmação e Decaindo. |
| `debug_view.py` | Janela Diagnóstico (mosaico ao vivo das etapas da detecção). |
| `floating.py` | Mover, redimensionar pela borda e fechar com Esc nos popups. |
| `settings.py` | Configurações (`data/settings.json`). |
| `graph_data.py`, `phone_config.py`, `phone_push.py` | Dados do gráfico, `phone.ini` e envio ao celular. |
| `app.py` | Interface Tk. |

## Como rodar

- Testes: `.venv/bin/python -m pytest -q` (sem janelas; `--gui` abre as dos testes de interface)
- App: `.venv/bin/python -m levainpeak.app --resume` (retoma a sessão com o CSV modificado mais recentemente).
- Reiniciar o app aberto: `kill $(pgrep -f levainpeak.app)` e subir de novo com `--resume`, em segundo plano.
- Câmera: `camera.ini` local (fora do git). Nunca versionar credenciais nem imagens da câmera.
- Sessões: `data/sessions/*.csv` e `.json` (fora do git). Cópias de segurança em
  `data/sessions/backup_antes_da_correcao_2026-10-30/`.

## Comportamentos que não são óbvios

- **Volume relativo:** fração da capacidade do pote, integrando a largura ao quadrado até a superfície.
  0% do gráfico é o nível inicial. 100% é o dobro, até ele ser alcançado, e depois o pico da sessão.
- **Base do pote (geometria 2):** o volume só conta o pote até onde o levain claro termina na base (a claridade
  despenca na borda do vidro, ~y=905), não até o fim da caixa (~930, que pega a tigela de aço). Isso e um nível
  inicial gravado com a medição antiga faziam o fator de crescimento sair ~14% abaixo do real (1,87 em vez de 2,14
  na mesma imagem). `GEOMETRY_VERSION` (em `sessions.py`) fica no JSON da sessão; ao retomar uma sessão de versão
  menor, o nível inicial é refeito uma vez a partir da marca. **Mudou a forma de medir? Suba a versão.**
- **Time-lapse** (`timelapse.py`): botão ao lado do "Girar". Janela com as fotos da sessão (menu das últimas 10 com
  nome, data e nº de fotos), barra, play, fps de 1 a 30 e teclas (↑ → espaço avançam; ↓ ← Shift+espaço voltam;
  Home/End; Esc). O intervalo das fotos é configurável (1 a 120 min), na sessão (`snapshot_seconds` no JSON) e global.
- **Previsão do dobro:** `last_forecast` (`at`, `basis`) vai para o JSON da sessão (no máximo a cada 60 s) e aparece
  como "(última conhecida)" quando não há tendência. No resume o histórico do CSV volta ao previsor com o relógio
  real. Com o dobro alcançado, o texto traz a hora (primeira linha do CSV com fator ≥ 2,0).
- **JSON da sessão:** `save_regions` faz merge e preserva `snapshot_seconds`, `last_forecast` e `volume_offset`.
- **Fases:** depois que a subida mínima aconteceu (`_risen`) a fase não volta a "Aquecendo".
- **Lâmpada:** segura o volume quando o brilho muda >12% (padrão 10 min, no máximo 3×). Se a luz mudou de vez, a
  série é emendada no nível em que foi segurada.
- **Gráfico:** passar o mouse mostra hora, fator (`1.00x`) e temperatura. Todo "Zerar" pede confirmação (o ângulo e "Imagem sem girar"); vale também para o ângulo da câmera futuro.
- **Quadro "Volume relativo":** mostra também o fator da leitura anterior gravada, com a contagem de repetições.
- **Defeito aberto:** o nível inicial acompanha as emendas da série e o fator oscila a cada reinício. A superfície
  lida fica ~30–40 px abaixo do topo visível da massa densa (contraste baixo com o pote de vidro).

- **Marca de início:** pode ser manual. Fica no JSON da sessão (`marker_y`) e volta ao retomar.
  O nível inicial da retomada vem da última linha do CSV.
- **Perfis:** em infravermelho a imagem é cinza (cor média 0,00 contra ~10 de dia). O topo e o fundo do pote
  são medidos de dia e reaproveitados à noite (`vessel_bounds`, no JSON). O OCR usa realce de nitidez.
- **Limpeza de dados:** salto de volume só vale se durar uns 20 s. Uma leitura abaixo de um nível já alcançado,
  e depois superada por outra, é inválida (`valid_after_recovery`). Temperatura não lida repete a última boa.
- **Fases do gráfico:** recalculadas dos dados limpos. "Pico" só aparece num platô confirmado antes da queda.
- **OCR:** só 2, 1, 9 e 8 foram validados em quadros reais. Em infravermelho só o 8. Um 3 lido como 8 aparece
  com borrão forte em teste sintético.
- **Interface:** o vídeo é reduzido à resolução da tela antes de processar. O gráfico só é recalculado quando o
  CSV muda. Os cartões reservam 3 linhas e a mensagem 2.
- **Contorno desenhado:** o app confia no polígono (sem teste de "pote vazio"). A superfície é achada subindo do
  fundo, com corte próprio para dia e infravermelho (`_find_surface_from_below`). Refazer o contorno mantém a caixa
  do pote que o usuário desenhou, se ela ainda contiver o contorno novo.
- **Alinhamento do pote (`alignment.py`):** uma referência de bordas por perfil (`data/pot_reference_<perfil>.npz`).
  Contorno, caixa e marca acompanham o pote quando ele é movido; abaixo de `MIN_SCORE` (0,70) a gravação pausa
  ("pote não localizado"). A referência é só uma faixa ao longo das paredes e da base, mais a tampa: o fundo visto
  através do vidro não anda com o pote e, incluído, segurava o encontro no lugar antigo (`VERSION` do `.npz`
  invalida referências antigas).
- **Marca "+" ou "X" (`target.py`, opcional):** liga no menu de botão direito do vídeo. Só de dia, só traço escuro
  sobre fundo claro. Com as bordas achadas corrige a altura e guarda quanto a etiqueta andou de lado (giro);
  sem as bordas move o desenho também na horizontal, descontando esse giro. Desenha um círculo ou quadrado
  vermelho translúcido (configurável) quando detecta.
- **Emenda do volume:** a cada troca dia/IR e ao retomar, o volume novo é juntado ao último (`_join_volume`).
  Sessão nova zera isso (`reset_join`) e o nível inicial anda junto com o volume, para a marca ler 1,00×.
  Sessão retomada herda a emenda; as marcações (pote, contorno, termômetro, visor, marca) ficam só no JSON da sessão; sem sessão o app abre sem marcações (`data/regions.json` não é mais usado).
- **Diagnóstico:** o painel 5 mostra o contorno desenhado (ciano) e, em magenta, as paredes que a busca
  automática acharia sem ele. Em pote cônico ela não acha nada, e o painel diz isso.
- **Apps Script:** `versao()` no editor e `?o=versao` na implantação dizem qual `Code.gs` está publicado.
- **Snapshots:** a cada 10 min o quadro bruto vai para `data/sessions/<sessão>_fotos/`.
- **Visor de dia:** o corte do OCR fica dentro da faixa de brilho do próprio visor. Piso fixo de 100 lia "888"
  quando o LED escurecia o fundo da face.

## Preferências de trabalho do usuário

- Não edita o código enquanto o assistente trabalha. O nvim reformata ao salvar. Conferir `git status` e
  `git diff` no começo de cada rodada. Commitar só os arquivos próprios, nunca `git add -A`.
- Reiniciar o app com `--resume` sempre que for preciso. Com duas instâncias, `--resume NOME` retoma a sessão pelo nome
  (ou nome do arquivo; vale também parte do nome; a mais nova se houver várias). Sem NOME retoma a mais nova.

## Celular: o app envia para uma planilha e um painel lê

Implementado (2026-10-31). O fluxo: o app do PC envia **um pacote por ciclo** para o Google Sheets, por um
Apps Script; um painel estático só de leitura mostra os dados.

| Peça | Onde |
|---|---|
| Receptor (Apps Script) | `docs/apps-script/Code.gs`; simulação em `docs/apps-script/simulacao.js` (`node docs/apps-script/simulacao.js`) |
| Envio no app | `src/levainpeak/phone_push.py` (`PhonePusher`, `build_packet`, `make_snapshot`), ligado em `app.py` |
| Configuração secreta | `phone.ini` (`[phone]`, `url` e `token`; fora do git; modelo `phone.ini.example`); leitura em `phone_config.py` |
| Opções | aba Configurações: ligar, intervalo (60 a 900 s, padrão 120), imagem do pote (desligada por padrão); `settings.py` |
| Dados do gráfico | `graph_data.py` (`prepare_graph_data`, `volume_scale`, `chart_payload`), usado pela janela e pelo painel |
| Painel | `painel/` (HTML, JS e CSS sem biblioteca). Rodar: `cd painel && python -m http.server 8000`. Links prontos: `python -m levainpeak.phone_config` |

- **Pacote:** `{token, session, sent_at, live, points, chart, snapshot}`. `points` = `{t, v, c, p}` (só os novos desde
  o último envio); `chart` = série já na escala do app (0% = início, 100% = dobro ou pico), até 400 pontos, com as
  fases; `snapshot` = recorte do pote em base64, abaixo de 25.000 caracteres. `live` = `sessao, volume_pct,
  crescimento, fase, temperatura_c, temperatura_nota, previsao, perfil, status, intervalo_s`.
- **Leitura:** `GET ?token=...&o=painel[&snap=<t>]` devolve estado, gráfico e snapshot numa chamada; o snapshot só
  vai se mudou. O painel guarda o endereço e o token no `localStorage` (entram pelo `#u=...&t=...`, que não vai
  para o servidor) e atualiza a cada 60 s.
- **Implantação do Apps Script:** "Executar como: Eu", "Quem pode acessar: Qualquer pessoa". **Depois de editar o
  `Code.gs`, criar uma nova versão** em "Gerenciar implantações". Se o painel disser que o script está
  desatualizado, é isso.
- **Cuidados:** o `POST` ao Apps Script é respondido com um redirecionamento 302 (o `urllib` já segue; no `curl`,
  `-L -d ...` sem `-X POST`). O token nunca vai para log nem para a mensagem de status. O token colado em
  conversa deve ser trocado (propriedade `TOKEN` do script e `phone.ini`).
- **Testes:** `pytest -q` não abre janela. `pytest --gui` roda os testes que abrem janelas e tomam o foco.
- **Descartados:** Firebase (o "ao vivo" de verdade não compensa com envio a cada 2 a 4 min), Gist, página do
  claude.ai.

Validado contra a planilha real em 2026-10-31: envio aceito (1000 pontos), `chart` gravado, `o=painel` lido e o
painel renderizado no Chrome headless com esses dados. A implantação em uso é a criada em "Nova implantação"
(o `/exec` muda a cada nova implantação; editar a existente e criar **nova versão** mantém a URL). A antiga deve ser
arquivada.

Pendente: o usuário ligar o envio na aba Configurações; apagar a linha de teste ("teste", 01/01) da aba `serie`;
abrir o painel de novo pelo link do `python -m levainpeak.phone_config` nos aparelhos que guardaram a URL antiga.
Ainda não vistos: o painel no celular de verdade e o tema escuro pela preferência do sistema.

## Publicação no GitHub

Publicado em 2026-10-31 em **github.com/lexrupy/levainpeak** (público, MIT). Remoto `origin` por SSH.

- **Antes de publicar:** auditoria de segredos em todo o histórico (token, IDs do Apps Script, IP da câmera, e-mail,
  caminhos: 0 ocorrências) e **reescrita do e-mail dos 55 commits** para o noreply do GitHub
  (`16597+lexrupy@users.noreply.github.com`), com o conteúdo idêntico (mesmo hash de árvore). O `user.email` local
  deste repositório é esse noreply. Backup do histórico original (com o e-mail antigo, **não publicar**):
  `~/projects/levainpeak-antes-da-publicacao.bundle`. O histórico publicado não deve ser reescrito de novo.
- **Nunca versionar:** `phone.ini`, `camera.ini`, `.envrc`, `data/` (todos no `.gitignore`).
- **Painel no github.io:** `https://lexrupy.github.io/levainpeak/painel/`. O Pages está ligado por **branch**
  (Source: Deploy from a branch, `main`, raiz), então serve o repositório inteiro: a raiz mostra o README e o painel
  fica em `/painel/`. Cada push que muda `painel/` republica sozinho (workflow "pages build and deployment", do próprio
  GitHub). Não há workflow nosso: o que tentamos para o modo "GitHub Actions" foi removido por não ser o modo em uso.
- **Link do painel:** `panel_url = https://lexrupy.github.io/levainpeak/painel/` no `phone.ini`. Na aba Configurações há
  dois botões: "Copiar link do painel local" (precisa de `cd painel && python -m http.server 8000`) e "... online".
  Verificado: o painel publicado lê a planilha real a partir do github.io (HTTPS, CORS ok).

## Limites conhecidos

- A leitura do termômetro em infravermelho foi ajustada em um único valor (21.8).
- A janela mínima exige ~903 px. O gerenciador de janelas do usuário (tiling) pode abrir a janela mais estreita.
- O gráfico lê as fases recalculadas, mas o CSV guarda os estados detalhados do rastreador.

## Pendências (1/11/2026)

- Afrouxar o filtro de saltos do volume: 94% das linhas repetem o valor anterior, e a série sai em degraus.
- Fases: exigir subida mínima antes de aceitar platô/pico (≥1,3× ou ≥0,06) e registrar o pico retroativamente na
  hora do máximo suavizado. Simulação em `sim_pico.py` (scratchpad, não versionada): falsos alarmes de platô na fase
  lenta e "Queda em confirmação" por degrau de -0,02.
- Lente de +8 a +10 dioptrias sobre a câmera (foco a 10 cm). Depois: redesenhar contorno, caixa, marca e visor, e
  rever os cortes de superfície (IR 0,40, dia 0,65).
- Publicar o `Code.gs` novo no Apps Script, limpar a aba `serie` e trocar o token (sem colar no chat).
- Opcionais: faixa de aviso sobre o vídeo, `data/` no `.gitignore`, marca de polaridade invertida, salvar gráfico
  como PNG.

## Próxima funcionalidade: geometria do pote e ângulo da câmera (decidido em 2/11/2026)

Pote novo medido (interno): topo 7,0 cm, fundo 4,1 cm, parede em ângulo ~5,1 cm, altura vertical ~4,9 cm,
capacidade ~120 mL; a metade da altura guarda só ~37% do volume.

1. Campos opcionais topo, fundo e altura em Configurações (padrão global e por sessão no JSON). O volume usa o
   tronco de cone real e mostra mL.
2. Sem contorno desenhado, um trapézio na proporção certa surge na tela para encaixar (ou é detectado). A sessão
   atual conta como já ajustada.
3. Ângulo da câmera: correção de perspectiva (keystone) vertical, e horizontal opcional, com sugestão automática
   pela proporção do pote. Ajustável no meio da sessão: coordenadas gravadas no quadro original, correção aplicada
   na hora, marca recalculada na geometria nova, série emendada no instante (como a lâmpada) e cada mudança gravada
   no JSON com hora e valor. Os pontos antigos ficam na escala antiga; reprocessar pelas fotos seria ferramenta à parte.

Ideias guardadas: rede pequena, silhueta da massa (área^1,5), leitura junto às paredes, fundo preto fosco, pote de
plástico fosco, marca física no pote, fotos a cada 2 min.

## Painel do celular: sessões, comparação e quadros (plano combinado em 03/11/2026)

Nada disto está implementado. A ordem é a das etapas; cada uma com testes e push próprios.

1. **Três sessões na planilha.** Uma aba de série, de quadros e de "ao vivo" por sessão; a 4ª apaga a mais antiga. Resolve duas instâncias ao mesmo tempo (hoje uma apaga a outra). O `Code.gs` muda: o usuário cola a versão nova e reimplanta (URL e token iguais). Seletor de sessão no painel, lembrando a última escolha.
2. **Comparar no painel.** O painel lê as 3 sessões (rota nova, carga compacta do gráfico de cada uma) e sobrepõe as curvas de crescimento alinhadas pelo tempo desde a mistura, com cada sessão ligada ou desligada. Depois, se fizer sentido, os potes lado a lado.
3. **Quadros rasterizados.** Um quadro por atualização (2 min), ~0,55 KB: grade 32 × 22, 16 cores por quadro, só as células abaixo da superfície, média de 3 quadros vizinhos contra o ruído. Uma linha por quadro na aba da sessão; o painel pede só os mais novos que o último que tem, guarda no celular e começa pelos ~120 últimos. O snapshot real continua como está, para conferir medidas erradas. Opção a avaliar: topo com a curva lida da foto (16 números por quadro, média igual ao nível medido).

Protótipo visual (A chapada, B rasterizada, C formato do topo): artefato privado; os dados vieram das 15 últimas fotos da sessão `Levain 03/11 21:32`. Medidas: B ≈ 8 KB para 15 quadros; C ≈ 1,4 KB para 15 quadros.
