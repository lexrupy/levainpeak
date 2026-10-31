# LevainPeak — estado do projeto

Nota para retomar numa conversa nova. Atualizada em 2026-10-31, com 81 testes passando
e o último commit em `4feb7e1`. Complementa o `AGENTS.md` (regras do repositório) e o `README.md`.

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

## Em andamento: acompanhar pelo celular (NÃO implementado)

O usuário pediu para **não implementar ainda**. Decisões já tomadas:

- **Destino dos dados:** Firebase Realtime Database. O app do PC envia (push) por HTTPS e um **PWA só de leitura**
  lê de lá. Funciona fora de casa e com o PC desligado (mostra o último dado).
- **Um pacote por ciclo, com tudo:** estado de agora (volume, fase, temperatura, previsão), pontos novos da série
  e o snapshot. **Intervalo configurável** na aba Configurações, com 120 s como padrão e 240 s como alternativa.
  Sem envio imediato nos eventos.
- **Snapshot:** só o recorte do pote, reduzido (~300 px, JPEG ~60), em base64, **abaixo de ~25 KB**. Opção
  separada, desligada por padrão.
- **PWA:** mostra "atualizado há N min". Hospedagem estática (por exemplo Firebase Hosting).
- **Descartados:** página do claude.ai (o app não consegue gravar nela sozinho), Gist (revisões demais a cada
  envio, cache de minutos), Google Sheets (sem imagem e sem ao vivo).

Pendente de decidir ou de fazer pelo usuário:

1. Quem pode ler: token no PWA ou caminho com chave aleatória longa.
2. O usuário criar o projeto Firebase e o banco (posso escrever um passo a passo curto) e guardar a chave de
   escrita em configuração local, fora do git.
3. Quanto da série enviar (sugestão: as últimas N horas, para o banco não crescer sem limite).

Plano técnico sugerido: extrair o preparo dos dados do gráfico (`App._prepare_graph_data` e a escala do eixo)
para uma função pura compartilhada pela janela e pelo PWA. Depois: um módulo de envio com um destino plugável,
as opções na aba Configurações e o PWA estático.

## Limites conhecidos

- A leitura do termômetro em infravermelho foi ajustada em um único valor (21.8).
- A janela mínima exige ~903 px. O gerenciador de janelas do usuário (tiling) pode abrir a janela mais estreita.
- O gráfico lê as fases recalculadas, mas o CSV guarda os estados detalhados do rastreador.
