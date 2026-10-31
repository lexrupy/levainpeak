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
| `graph_phases.py` | Fases do gráfico: Fase inicial, Crescimento, Pico, Declínio. |
| `debug_view.py` | Janela Diagnóstico (mosaico ao vivo das etapas da detecção). |
| `floating.py` | Mover, redimensionar pela borda e fechar com Esc nos popups. |
| `settings.py` | Configurações (`data/settings.json`). |
| `app.py` | Interface Tk. |

## Como rodar

- Testes: `.venv/bin/python -m pytest -q`
- App: `.venv/bin/python -m levainpeak.app --resume` (retoma a sessão com o CSV modificado mais recentemente).
- Reiniciar o app aberto: `kill $(pgrep -f levainpeak.app)` e subir de novo com `--resume`, em segundo plano.
- Câmera: `camera.ini` local (fora do git). Nunca versionar credenciais nem imagens da câmera.
- Sessões: `data/sessions/*.csv` e `.json` (fora do git). Cópias de segurança em
  `data/sessions/backup_antes_da_correcao_2026-10-30/`.

## Comportamentos que não são óbvios

- **Volume relativo:** fração da capacidade do pote, integrando a largura ao quadrado até a superfície.
  0% do gráfico é o nível inicial. 100% é o dobro, até ele ser alcançado, e depois o pico da sessão.
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

## Preferências de trabalho do usuário

- Não edita o código enquanto o assistente trabalha. O nvim reformata ao salvar. Conferir `git status` e
  `git diff` no começo de cada rodada. Commitar só os arquivos próprios, nunca `git add -A`.
- Reiniciar o app com `--resume` sempre que for preciso.

## Em andamento: acompanhar pelo celular (app NÃO implementado)

O usuário pediu para **não implementar o app ainda**. Decisões já tomadas:

- **Destino dos dados:** Google Sheets, recebendo por um **Google Apps Script** (app da web). O app do PC faz um
  `POST` HTTPS simples (biblioteca padrão, sem OAuth nem chave). O celular lê por `GET` ou abre a planilha.
  Firebase foi descartado porque, com envio a cada 2 a 4 minutos, o "ao vivo" de verdade não compensa o setup.
- **Script pronto:** `docs/apps-script/Code.gs` (validado só por sintaxe e por uma simulação em memória; ainda
  **não** foi rodado no Google). O usuário cola o script na planilha, define a propriedade `TOKEN`, roda
  `configurar()` e implanta como app da web ("Executar como: eu", "Acesso: qualquer pessoa").
- **Um pacote por ciclo, com tudo:** `POST` com `{token, session, sent_at, live, points, snapshot}`.
  `points` = `{t, v (0-1), c, p}`; `snapshot` = `{t, jpeg_b64}`. **Intervalo configurável** na aba Configurações,
  120 s por padrão e 240 s como alternativa. Sem envio imediato nos eventos.
- **Snapshot:** só o recorte do pote, ~300 px, JPEG ~60, em base64 **abaixo de ~25 KB** (o script recusa acima de
  49.000 caracteres). Opção separada, desligada por padrão.
- **Leitura:** `GET ?token=...&o=ao_vivo | serie&limite=N | snapshot`. A primeira versão pode dispensar o PWA
  (olhar a planilha e um gráfico do Sheets no celular). O PWA entra para mostrar a imagem e o gráfico do app.
- **Descartados:** página do claude.ai (o app não grava nela sozinho), Gist (revisões demais, cache de minutos).
- **Cuidados do cliente Python:** o Apps Script responde ao `POST` com um redirecionamento 302 que deve ser
  seguido com `GET` (o `urllib` já faz isso). O token vai no corpo do `POST` e na URL do `GET`. Depois de editar
  o script é preciso criar uma **nova versão** da implantação.

Pendente: o usuário rodar o script e testar com `curl`; decidir quem lê (token no PWA); depois implementar o
envio no app (módulo de envio, opções em Configurações) e, se quiser, o PWA. Plano técnico: extrair o preparo
dos dados do gráfico (`App._prepare_graph_data` e a escala do eixo) para uma função pura compartilhada.

## Limites conhecidos

- A leitura do termômetro em infravermelho foi ajustada em um único valor (21.8).
- A janela mínima exige ~903 px. O gerenciador de janelas do usuário (tiling) pode abrir a janela mais estreita.
- O gráfico lê as fases recalculadas, mas o CSV guarda os estados detalhados do rastreador.
