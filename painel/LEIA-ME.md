# Painel do celular

Página só de leitura que mostra o levain: números de agora, gráfico (volume e temperatura, com as fases) e a
última imagem do pote. Lê a planilha pelo Apps Script (`docs/apps-script/Code.gs`), que o app do PC alimenta.

## Rodar

```sh
cd painel
python -m http.server 8000
```

No PC: `http://localhost:8000/`. No celular, na mesma rede: `http://<ip-do-pc>:8000/`.

Na primeira visita, abra o endereço com a configuração depois do `#` (ela **não** vai para o servidor, só fica no
navegador). O comando abaixo, rodado na raiz do projeto, imprime os links prontos a partir do `phone.ini`:

```sh
python -m levainpeak.phone_config
```

Depois da primeira vez, o endereço e o token ficam guardados no aparelho (`localStorage`) e a página abre sozinha.
Não compartilhe esses links: eles carregam o token.

## Publicado

`https://lexrupy.github.io/levainpeak/painel/`: o GitHub Pages serve o repositório (deploy pela branch `main`), então
cada push que muda `painel/` atualiza o site sozinho, em cerca de 1 minuto. É HTTPS, o que permite instalar como app.

## O que a página faz

- Atualiza a cada 60 s, ao voltar para a aba e ao tocar em ↻.
- Mostra "atualizado há N min". Passando de 3 ciclos de envio sem dados novos, avisa em vermelho.
- Sem conexão, mostra o último dado guardado.
- Tema claro e escuro automáticos. Funciona como página comum; "adicionar à tela inicial" cria um atalho.
  O modo instalável com cache offline só vale em `localhost` ou HTTPS (o `http://<ip>` da rede não permite).

## Segurança

Os arquivos desta pasta não contêm segredo. O token só existe no `localStorage` de cada aparelho e na propriedade
`TOKEN` do script. Se um link com o token vazar, troque a propriedade `TOKEN` do script e o `phone.ini`.
