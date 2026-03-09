# LoL Match Predictor

Scraper de dados de partidas ranqueadas de League of Legends e preditor de vitória em tempo real usando Machine Learning.

Projeto desenvolvido como Trabalho de Conclusão de Curso (TCC) na COPPE/UFRJ.

## Visão Geral

O projeto consiste em três componentes:

1. **Pipeline de coleta de dados** — Scripts que coletam dados de partidas ranqueadas via Riot Games API
2. **Modelo de Machine Learning** — LightGBM otimizado com calibração isotônica, treinado em ~160k partidas
3. **Overlay em tempo real** — Aplicação desktop que exibe a probabilidade de vitória durante uma partida ao vivo

## Pipeline de Coleta

Três scripts executados sequencialmente. Cada um suporta retomada incremental.

```
1. python pythoncode/matchIDscraper.py           → puuid.csv
2. python pythoncode/collect_match_ids.py         → MatchIds.csv
3. python pythoncode/collect_match_timeline.py    → MatchTimelineAllMinutes.csv
```

No Windows, `coletar_dados.bat` executa os três estágios automaticamente.

### Configuração

Os scripts requerem uma chave da [Riot Games API](https://developer.riotgames.com/). Configure via variável de ambiente:

```bash
export RIOT_API_KEY="RGAPI-sua-chave-aqui"
```

> Chaves de desenvolvimento expiram a cada 24 horas.

## Overlay

Overlay nativo em PySide6 (Qt) que se conecta à [Live Client Data API](https://developer.riotgames.com/docs/lol#game-client-api) do League of Legends durante uma partida ativa.

### Funcionalidades

- Gauge semicircular animado com probabilidade de vitória
- Faixa lateral colorida por time (azul/vermelho) com efeito de pulso
- Recomendações de próximas ações ("what-if scenarios") com impacto estimado na probabilidade
- Draggable, sempre no topo, sem janela de console

### Executável

Baixe o `.exe` pré-compilado na seção [Releases](../../releases). Não requer instalação — basta executar.

### Rodando em modo desenvolvimento

```bash
cd overlay/native
pip install -r requirements.txt
python overlay.pyw
```

### Compilando o executável

```bash
cd overlay/native
python build_overlay.py
```

O executável será gerado em `overlay/native-dist/`.

## Modelo

O modelo treinado está em `models/`:

| Arquivo | Descrição |
|---------|-----------|
| `lol_win_predictor_lgbm_isotonic.joblib` | LightGBM + calibração isotônica |
| `features.joblib` | Lista das 31 features utilizadas |
| `best_params_lgbm.joblib` | Hiperparâmetros otimizados |

**Métricas:** Acurácia ~72.4%, ROC AUC ~0.817 (teste), treinado com top 30 features SHAP + tempo.

## Estrutura do Projeto

```
├── pythoncode/
│   ├── matchIDscraper.py          # Etapa 1: coleta PUUIDs
│   ├── collect_match_ids.py       # Etapa 2: coleta Match IDs
│   ├── collect_match_timeline.py  # Etapa 3: coleta timelines
│   └── live_game_scraper.py       # Mapeamento Live Client API → features
├── overlay/
│   └── native/
│       ├── overlay.pyw            # Overlay principal
│       ├── sample_overlay.pyw     # Demo com dados fictícios
│       ├── build_overlay.py       # Script de build (PyInstaller)
│       └── requirements.txt       # Dependências Python
├── models/                        # Modelo treinado (LightGBM)
└── coletar_dados.bat              # Pipeline completo (Windows)
```

## Tecnologias

- **Coleta:** Python, Riot Games API (League V4, Match V5), Data Dragon
- **Modelo:** LightGBM, scikit-learn, SHAP, Polars, Pandas
- **Overlay:** PySide6 (Qt), PyInstaller

## Licença

Este projeto foi desenvolvido para fins acadêmicos.

*League of Legends e Riot Games são marcas registradas da Riot Games, Inc.*
