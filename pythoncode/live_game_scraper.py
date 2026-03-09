# Script para coletar dados em tempo real de uma partida local de League of Legends
# Usa a Live Client Data API (disponível apenas durante uma partida ativa)
#
# Endpoints disponíveis:
# - /liveclientdata/allgamedata - Todos os dados combinados
# - /liveclientdata/activeplayer - Stats do jogador ativo
# - /liveclientdata/playerlist - Lista de todos os jogadores
# - /liveclientdata/gamestats - Estado geral do jogo
# - /liveclientdata/eventdata - Eventos (kills, dragons, etc.)
#
# Uso:
#   python live_game_scraper.py              # Coleta snapshot único
#   python live_game_scraper.py --compare    # Compara variáveis com Match-V5

import os
import sys
import json
import argparse
from datetime import datetime
from typing import Optional, Dict, Any, List, Set

import requests
import urllib3

# Desabilita warnings de SSL (certificado auto-assinado do cliente LoL)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ============================================================================
# CONFIGURAÇÕES
# ============================================================================

# URL base da Live Client Data API
BASE_URL = "https://127.0.0.1:2999"

# Endpoints disponíveis
ENDPOINTS = {
    "all": "/liveclientdata/allgamedata",
    "active_player": "/liveclientdata/activeplayer",
    "player_list": "/liveclientdata/playerlist",
    "game_stats": "/liveclientdata/gamestats",
    "events": "/liveclientdata/eventdata",
}

# Diretório de output
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "Data")


# ============================================================================
# FUNÇÕES PARA CARREGAR VARIÁVEIS DO DATASET DE TREINAMENTO
# ============================================================================

# Caminho para o arquivo CSV com exemplo do dataset de treinamento
TRAINING_DATA_CSV = os.path.join(os.path.dirname(os.path.dirname(__file__)), "training_data_head.csv")


def load_training_columns() -> List[str]:
    """
    Carrega as colunas do dataset de treinamento a partir do CSV de exemplo.
    
    Returns:
        Lista de nomes das colunas
    """
    if not os.path.exists(TRAINING_DATA_CSV):
        print(f"[WARN] Arquivo {TRAINING_DATA_CSV} não encontrado.")
        print("       Usando lista de variáveis padrão.")
        return []
    
    try:
        with open(TRAINING_DATA_CSV, 'r', encoding='utf-8') as f:
            header = f.readline().strip()
            columns = header.split(',')
            return columns
    except Exception as e:
        print(f"[WARN] Erro ao ler {TRAINING_DATA_CSV}: {e}")
        return []


def categorize_training_columns(columns: List[str]) -> Dict[str, List[str]]:
    """
    Categoriza as colunas do dataset de treinamento.
    
    Args:
        columns: Lista de nomes de colunas
    
    Returns:
        Dicionário com colunas categorizadas
    """
    categories = {
        "metadata": [],
        "player_stats": [],  # p1_*, p2_*, ..., p10_*
        "team_aggregates": [],  # blue_*, red_*, *Diff
        "derived": []  # Per min, ratios, scores
    }
    
    for col in columns:
        # Metadados
        if col in ["matchId", "Region", "blueWin", "time", "timestamp"]:
            categories["metadata"].append(col)
        # Stats por jogador
        elif col.startswith(("p1_", "p2_", "p3_", "p4_", "p5_", 
                            "p6_", "p7_", "p8_", "p9_", "p10_")):
            categories["player_stats"].append(col)
        # Features derivadas (per min, ratios)
        elif "PerMin" in col or "ratio" in col.lower() or "Score" in col:
            categories["derived"].append(col)
        # Agregados por time
        elif col.startswith(("blue_", "red_")) or col.endswith("Diff"):
            categories["team_aggregates"].append(col)
    
    return categories


def get_unique_player_stats(columns: List[str]) -> List[str]:
    """
    Extrai os nomes únicos das estatísticas de jogador (sem o prefixo p1_, p2_, etc).
    
    Args:
        columns: Lista de nomes de colunas
    
    Returns:
        Lista de nomes únicos de stats (ex: ['level', 'totalGold', ...])
    """
    unique_stats = set()
    
    for col in columns:
        for i in range(1, 11):
            prefix = f"p{i}_"
            if col.startswith(prefix):
                stat_name = col[len(prefix):]
                unique_stats.add(stat_name)
                break
    
    return sorted(list(unique_stats))


# ============================================================================
# FUNÇÕES DE CONEXÃO COM A API
# ============================================================================

def check_game_active() -> bool:
    """
    Verifica se há uma partida ativa no cliente local.
    
    Returns:
        True se há partida ativa, False caso contrário
    """
    try:
        response = requests.get(
            f"{BASE_URL}{ENDPOINTS['game_stats']}",
            verify=False,
            timeout=2
        )
        return response.status_code == 200
    except requests.exceptions.RequestException:
        return False


def get_live_data(endpoint: str = "all") -> Optional[Dict[str, Any]]:
    """
    Busca dados da Live Client Data API.
    
    Args:
        endpoint: Nome do endpoint (all, active_player, player_list, game_stats, events)
    
    Returns:
        Dados JSON da API ou None se falhar
    """
    if endpoint not in ENDPOINTS:
        print(f"[ERRO] Endpoint inválido: {endpoint}")
        print(f"       Endpoints válidos: {list(ENDPOINTS.keys())}")
        return None
    
    url = f"{BASE_URL}{ENDPOINTS[endpoint]}"
    
    try:
        print(f"[INFO] Conectando a {url}...")
        response = requests.get(url, verify=False, timeout=5)
        
        if response.status_code == 200:
            data = response.json()
            print(f"[OK] Dados recebidos com sucesso!")
            return data
        else:
            print(f"[ERRO] HTTP {response.status_code}")
            return None
            
    except requests.exceptions.ConnectionError:
        print("[ERRO] Não foi possível conectar ao cliente do LoL.")
        print("       Verifique se você está em uma partida ativa.")
        return None
    except requests.exceptions.Timeout:
        print("[ERRO] Timeout ao conectar ao cliente do LoL.")
        return None
    except json.JSONDecodeError:
        print("[ERRO] Resposta não é um JSON válido.")
        return None
    except Exception as e:
        print(f"[ERRO] Erro inesperado: {e}")
        return None


# ============================================================================
# FUNÇÕES DE EXTRAÇÃO DE VARIÁVEIS
# ============================================================================

def extract_all_keys(data: Any, prefix: str = "") -> Set[str]:
    """
    Extrai recursivamente todas as chaves de um objeto JSON.
    
    Args:
        data: Dados JSON (dict, list, ou valor primitivo)
        prefix: Prefixo para chaves aninhadas
    
    Returns:
        Set de todas as chaves encontradas
    """
    keys = set()
    
    if isinstance(data, dict):
        for key, value in data.items():
            full_key = f"{prefix}.{key}" if prefix else key
            keys.add(full_key)
            keys.update(extract_all_keys(value, full_key))
    elif isinstance(data, list) and len(data) > 0:
        # Para listas, usa [0] como placeholder
        keys.add(f"{prefix}[]")
        keys.update(extract_all_keys(data[0], f"{prefix}[]"))
    
    return keys


def categorize_live_api_vars(data: Dict[str, Any]) -> Dict[str, List[str]]:
    """
    Categoriza as variáveis disponíveis na Live Client API.
    
    Args:
        data: Dados completos da API (endpoint /allgamedata)
    
    Returns:
        Dicionário com variáveis categorizadas
    """
    categories = {
        "activePlayer": [],
        "allPlayers": [],
        "events": [],
        "gameData": []
    }
    
    # Active Player (jogador local)
    if "activePlayer" in data:
        active = data["activePlayer"]
        categories["activePlayer"] = list(extract_all_keys(active, "activePlayer"))
    
    # All Players (todos os 10 jogadores)
    if "allPlayers" in data and len(data["allPlayers"]) > 0:
        player = data["allPlayers"][0]
        categories["allPlayers"] = list(extract_all_keys(player, "player"))
    
    # Events (kills, objectives, etc.)
    if "events" in data and "Events" in data["events"]:
        if len(data["events"]["Events"]) > 0:
            event = data["events"]["Events"][0]
            categories["events"] = list(extract_all_keys(event, "event"))
    
    # Game Data (estado do jogo)
    if "gameData" in data:
        categories["gameData"] = list(extract_all_keys(data["gameData"], "gameData"))
    
    return categories


# ============================================================================
# FUNÇÕES DE COMPARAÇÃO COM DATASET DE TREINAMENTO
# ============================================================================

def compare_with_training_data(live_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compara as variáveis da Live Client API com as colunas do dataset de treinamento.
    
    Args:
        live_data: Dados da Live Client API
    
    Returns:
        Relatório de comparação detalhado
    """
    # Carrega colunas do CSV de treinamento
    training_columns = load_training_columns()
    
    if not training_columns:
        return {"error": "Não foi possível carregar as colunas do dataset de treinamento"}
    
    # Categoriza as colunas
    categories = categorize_training_columns(training_columns)
    unique_player_stats = get_unique_player_stats(training_columns)
    
    report = {
        "training_data_info": {
            "total_columns": len(training_columns),
            "metadata_columns": len(categories["metadata"]),
            "player_stat_columns": len(categories["player_stats"]),
            "team_aggregate_columns": len(categories["team_aggregates"]),
            "derived_columns": len(categories["derived"]),
            "unique_player_stats": len(unique_player_stats)
        },
        "column_categories": categories,
        "unique_player_stats": unique_player_stats,
        "mappings": {
            "available": {},
            "partial": {},
            "not_available": {},
            "calculable": {}
        },
        "summary": {}
    }
    
    # Mapeamentos conhecidos entre Dataset de Treinamento e Live Client API
    # Formato: "stat_name": {"live_api_path": "...", "status": "available|partial|not_available|calculable", "notes": "..."}
    known_mappings = {
        # === STATS DISPONÍVEIS PARA TODOS OS JOGADORES ===
        "level": {
            "live_api_path": "allPlayers[].level",
            "status": "available",
            "notes": "Disponível para todos os 10 jogadores"
        },
        "totalCS": {
            "live_api_path": "allPlayers[].scores.creepScore",
            "status": "available",
            "notes": "CS total (minions + jungle)"
        },
        
        # === STATS DISPONÍVEIS APENAS PARA O JOGADOR ATIVO ===
        "currentGold": {
            "live_api_path": "activePlayer.currentGold",
            "status": "partial",
            "notes": "Apenas para o jogador local (activePlayer)"
        },
        "abilityHaste": {
            "live_api_path": "activePlayer.championStats.abilityHaste",
            "status": "partial",
            "notes": "Apenas para o jogador local"
        },
        "abilityPower": {
            "live_api_path": "activePlayer.championStats.abilityPower",
            "status": "partial",
            "notes": "Apenas para o jogador local"
        },
        "armor": {
            "live_api_path": "activePlayer.championStats.armor",
            "status": "partial",
            "notes": "Apenas para o jogador local"
        },
        "armorPen": {
            "live_api_path": "activePlayer.championStats.armorPenetrationFlat",
            "status": "partial",
            "notes": "Apenas para o jogador local"
        },
        "armorPenPercent": {
            "live_api_path": "activePlayer.championStats.armorPenetrationPercent",
            "status": "partial",
            "notes": "Apenas para o jogador local"
        },
        "attackDamage": {
            "live_api_path": "activePlayer.championStats.attackDamage",
            "status": "partial",
            "notes": "Apenas para o jogador local"
        },
        "attackSpeed": {
            "live_api_path": "activePlayer.championStats.attackSpeed",
            "status": "partial",
            "notes": "Apenas para o jogador local"
        },
        "bonusArmorPenPercent": {
            "live_api_path": "activePlayer.championStats.bonusArmorPenetrationPercent",
            "status": "partial",
            "notes": "Apenas para o jogador local"
        },
        "bonusMagicPenPercent": {
            "live_api_path": "activePlayer.championStats.bonusMagicPenetrationPercent",
            "status": "partial",
            "notes": "Apenas para o jogador local"
        },
        "ccReduction": {
            "live_api_path": "activePlayer.championStats.tenacity",
            "status": "partial",
            "notes": "Apenas para o jogador local (tenacity = ccReduction)"
        },
        "cooldownReduction": {
            "live_api_path": "activePlayer.championStats.cooldownReduction",
            "status": "partial",
            "notes": "Apenas para o jogador local"
        },
        "health": {
            "live_api_path": "activePlayer.championStats.currentHealth",
            "status": "partial",
            "notes": "Apenas para o jogador local"
        },
        "healthMax": {
            "live_api_path": "activePlayer.championStats.maxHealth",
            "status": "partial",
            "notes": "Apenas para o jogador local"
        },
        "healthRegen": {
            "live_api_path": "activePlayer.championStats.healthRegenRate",
            "status": "partial",
            "notes": "Apenas para o jogador local"
        },
        "lifesteal": {
            "live_api_path": "activePlayer.championStats.lifesteal",
            "status": "partial",
            "notes": "Apenas para o jogador local"
        },
        "magicPen": {
            "live_api_path": "activePlayer.championStats.magicPenetrationFlat",
            "status": "partial",
            "notes": "Apenas para o jogador local"
        },
        "magicPenPercent": {
            "live_api_path": "activePlayer.championStats.magicPenetrationPercent",
            "status": "partial",
            "notes": "Apenas para o jogador local"
        },
        "magicResist": {
            "live_api_path": "activePlayer.championStats.magicResist",
            "status": "partial",
            "notes": "Apenas para o jogador local"
        },
        "movementSpeed": {
            "live_api_path": "activePlayer.championStats.moveSpeed",
            "status": "partial",
            "notes": "Apenas para o jogador local"
        },
        "omnivamp": {
            "live_api_path": "activePlayer.championStats.omnivamp",
            "status": "partial",
            "notes": "Apenas para o jogador local"
        },
        "physicalVamp": {
            "live_api_path": "activePlayer.championStats.physicalVamp",
            "status": "partial",
            "notes": "Apenas para o jogador local"
        },
        "power": {
            "live_api_path": "activePlayer.championStats.resourceValue",
            "status": "partial",
            "notes": "Apenas para o jogador local (mana/energy/etc)"
        },
        "powerMax": {
            "live_api_path": "activePlayer.championStats.resourceMax",
            "status": "partial",
            "notes": "Apenas para o jogador local"
        },
        "powerRegen": {
            "live_api_path": "activePlayer.championStats.resourceRegenRate",
            "status": "partial",
            "notes": "Apenas para o jogador local"
        },
        "spellVamp": {
            "live_api_path": "activePlayer.championStats.spellVamp",
            "status": "partial",
            "notes": "Apenas para o jogador local"
        },
        
        # === STATS NÃO DISPONÍVEIS NA LIVE API ===
        "totalGold": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Não disponível (apenas currentGold para activePlayer)"
        },
        "xp": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "XP não é exposto pela Live API"
        },
        "minionsKilled": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Apenas CS total disponível (não separado por tipo)"
        },
        "jungleMinionsKilled": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Apenas CS total disponível (não separado por tipo)"
        },
        "jungleCS": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Apenas CS total disponível"
        },
        "posX": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Posição no mapa não é exposta"
        },
        "posY": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Posição no mapa não é exposta"
        },
        "magicDamageDone": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Stats de dano não disponíveis na Live API"
        },
        "physicalDamageDone": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Stats de dano não disponíveis na Live API"
        },
        "trueDamageDone": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Stats de dano não disponíveis na Live API"
        },
        "totalDamageDone": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Stats de dano não disponíveis na Live API"
        },
        "magicDamageTaken": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Stats de dano recebido não disponíveis"
        },
        "physicalDamageTaken": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Stats de dano recebido não disponíveis"
        },
        "trueDamageTaken": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Stats de dano recebido não disponíveis"
        },
        "totalDamageTaken": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Stats de dano recebido não disponíveis"
        },
        
        # === EVENTOS (KDA, Objetivos) ===
        "kills": {
            "live_api_path": "allPlayers[].scores.kills",
            "status": "available",
            "notes": "Disponível para todos os jogadores"
        },
        "deaths": {
            "live_api_path": "allPlayers[].scores.deaths",
            "status": "available",
            "notes": "Disponível para todos os jogadores"
        },
        "assists": {
            "live_api_path": "allPlayers[].scores.assists",
            "status": "available",
            "notes": "Disponível para todos os jogadores"
        },
        
        # === TEMPO DE JOGO ===
        "time": {
            "live_api_path": "gameData.gameTime",
            "status": "available",
            "notes": "Tempo em segundos (converter para minutos)"
        },
        "timestamp": {
            "live_api_path": "gameData.gameTime * 1000",
            "status": "calculable",
            "notes": "Calculável a partir de gameTime"
        },
    }
    
    # Mapeamentos para variáveis agregadas de time
    team_var_mappings = {
        # === CALCULÁVEIS A PARTIR DE DADOS DISPONÍVEIS ===
        "blue_kills": {
            "live_api_path": "sum(allPlayers[0-4].scores.kills)",
            "status": "calculable",
            "notes": "Soma dos kills do time azul"
        },
        "red_kills": {
            "live_api_path": "sum(allPlayers[5-9].scores.kills)",
            "status": "calculable",
            "notes": "Soma dos kills do time vermelho"
        },
        "killDiff": {
            "live_api_path": "blue_kills - red_kills",
            "status": "calculable",
            "notes": "Diferença de kills"
        },
        "blue_deaths": {
            "live_api_path": "sum(allPlayers[0-4].scores.deaths)",
            "status": "calculable",
            "notes": "Soma das mortes do time azul"
        },
        "red_deaths": {
            "live_api_path": "sum(allPlayers[5-9].scores.deaths)",
            "status": "calculable",
            "notes": "Soma das mortes do time vermelho"
        },
        "blue_assists": {
            "live_api_path": "sum(allPlayers[0-4].scores.assists)",
            "status": "calculable",
            "notes": "Soma das assistências do time azul"
        },
        "red_assists": {
            "live_api_path": "sum(allPlayers[5-9].scores.assists)",
            "status": "calculable",
            "notes": "Soma das assistências do time vermelho"
        },
        "blue_totalCS": {
            "live_api_path": "sum(allPlayers[0-4].scores.creepScore)",
            "status": "calculable",
            "notes": "CS total do time azul"
        },
        "red_totalCS": {
            "live_api_path": "sum(allPlayers[5-9].scores.creepScore)",
            "status": "calculable",
            "notes": "CS total do time vermelho"
        },
        "csDiff": {
            "live_api_path": "blue_totalCS - red_totalCS",
            "status": "calculable",
            "notes": "Diferença de CS"
        },
        "blue_avgLevel": {
            "live_api_path": "avg(allPlayers[0-4].level)",
            "status": "calculable",
            "notes": "Nível médio do time azul"
        },
        "red_avgLevel": {
            "live_api_path": "avg(allPlayers[5-9].level)",
            "status": "calculable",
            "notes": "Nível médio do time vermelho"
        },
        "levelDiff": {
            "live_api_path": "blue_avgLevel - red_avgLevel",
            "status": "calculable",
            "notes": "Diferença de nível médio"
        },
        
        # === EVENTOS DE OBJETIVOS (via events) ===
        "blue_dragons": {
            "live_api_path": "count(events.Events[DragonKill, team=ORDER])",
            "status": "calculable",
            "notes": "Contar eventos DragonKill do time azul"
        },
        "red_dragons": {
            "live_api_path": "count(events.Events[DragonKill, team=CHAOS])",
            "status": "calculable",
            "notes": "Contar eventos DragonKill do time vermelho"
        },
        "dragonDiff": {
            "live_api_path": "blue_dragons - red_dragons",
            "status": "calculable",
            "notes": "Diferença de dragões"
        },
        "blue_barons": {
            "live_api_path": "count(events.Events[BaronKill, team=ORDER])",
            "status": "calculable",
            "notes": "Contar eventos BaronKill"
        },
        "red_barons": {
            "live_api_path": "count(events.Events[BaronKill, team=CHAOS])",
            "status": "calculable",
            "notes": "Contar eventos BaronKill"
        },
        "baronDiff": {
            "live_api_path": "blue_barons - red_barons",
            "status": "calculable",
            "notes": "Diferença de barons"
        },
        "blue_towers": {
            "live_api_path": "count(events.Events[TurretKilled, team=ORDER])",
            "status": "calculable",
            "notes": "Contar torres destruídas"
        },
        "red_towers": {
            "live_api_path": "count(events.Events[TurretKilled, team=CHAOS])",
            "status": "calculable",
            "notes": "Contar torres destruídas"
        },
        "towerDiff": {
            "live_api_path": "blue_towers - red_towers",
            "status": "calculable",
            "notes": "Diferença de torres"
        },
        "blue_heralds": {
            "live_api_path": "count(events.Events[HeraldKill, team=ORDER])",
            "status": "calculable",
            "notes": "Contar arautos"
        },
        "red_heralds": {
            "live_api_path": "count(events.Events[HeraldKill, team=CHAOS])",
            "status": "calculable",
            "notes": "Contar arautos"
        },
        "heraldDiff": {
            "live_api_path": "blue_heralds - red_heralds",
            "status": "calculable",
            "notes": "Diferença de arautos"
        },
        "blue_inhibitors": {
            "live_api_path": "count(events.Events[InhibKilled, team=ORDER])",
            "status": "calculable",
            "notes": "Contar inibidores destruídos"
        },
        "red_inhibitors": {
            "live_api_path": "count(events.Events[InhibKilled, team=CHAOS])",
            "status": "calculable",
            "notes": "Contar inibidores destruídos"
        },
        "blue_elderDragons": {
            "live_api_path": "count(events.Events[DragonKill, DragonType=Elder, team=ORDER])",
            "status": "calculable",
            "notes": "Contar Elder Dragons do time azul"
        },
        "red_elderDragons": {
            "live_api_path": "count(events.Events[DragonKill, DragonType=Elder, team=CHAOS])",
            "status": "calculable",
            "notes": "Contar Elder Dragons do time vermelho"
        },
        "elderDragonDiff": {
            "live_api_path": "blue_elderDragons - red_elderDragons",
            "status": "calculable",
            "notes": "Diferença de Elder Dragons"
        },
        "blue_voidgrubs": {
            "live_api_path": "count(events.Events[HordeKill, team=ORDER])",
            "status": "calculable",
            "notes": "Contar Voidgrubs do time azul"
        },
        "red_voidgrubs": {
            "live_api_path": "count(events.Events[HordeKill, team=CHAOS])",
            "status": "calculable",
            "notes": "Contar Voidgrubs do time vermelho"
        },
        "voidgrubDiff": {
            "live_api_path": "blue_voidgrubs - red_voidgrubs",
            "status": "calculable",
            "notes": "Diferença de Voidgrubs"
        },
        "blue_eliteMonsters": {
            "live_api_path": "sum(dragons, elders, barons, heralds, voidgrubs)",
            "status": "calculable",
            "notes": "Total de monstros épicos do time azul"
        },
        "red_eliteMonsters": {
            "live_api_path": "sum(dragons, elders, barons, heralds, voidgrubs)",
            "status": "calculable",
            "notes": "Total de monstros épicos do time vermelho"
        },
        "eliteMonsterDiff": {
            "live_api_path": "blue_eliteMonsters - red_eliteMonsters",
            "status": "calculable",
            "notes": "Diferença de monstros épicos"
        },
        
        # === NÃO DISPONÍVEIS ===
        "blue_totalGold": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Gold total não disponível na Live API"
        },
        "red_totalGold": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Gold total não disponível na Live API"
        },
        "goldDiff": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Não calculável sem gold total"
        },
        "blue_totalXP": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "XP não disponível na Live API"
        },
        "red_totalXP": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "XP não disponível na Live API"
        },
        "xpDiff": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Não calculável sem XP"
        },
        "blue_goldPerMin": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Requer gold total"
        },
        "red_goldPerMin": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Requer gold total"
        },
        "blue_csPerMin": {
            "live_api_path": "blue_totalCS / (gameTime / 60)",
            "status": "calculable",
            "notes": "CS por minuto calculável"
        },
        "red_csPerMin": {
            "live_api_path": "red_totalCS / (gameTime / 60)",
            "status": "calculable",
            "notes": "CS por minuto calculável"
        },
        "blue_xpPerMin": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Requer XP total"
        },
        "red_xpPerMin": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Requer XP total"
        },
        "blue_kd_ratio": {
            "live_api_path": "blue_kills / max(blue_deaths, 1)",
            "status": "calculable",
            "notes": "Calculável"
        },
        "red_kd_ratio": {
            "live_api_path": "red_kills / max(red_deaths, 1)",
            "status": "calculable",
            "notes": "Calculável"
        },
        "blue_objectiveScore": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Score customizado, precisa ser recalculado"
        },
        "red_objectiveScore": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Score customizado, precisa ser recalculado"
        },
        "objectiveScoreDiff": {
            "live_api_path": "N/A",
            "status": "not_available",
            "notes": "Score customizado, precisa ser recalculado"
        },
    }
    
    # Processa cada variável única de jogador
    for stat in unique_player_stats:
        if stat in known_mappings:
            mapping = known_mappings[stat]
            report["mappings"][mapping["status"]][stat] = mapping
        else:
            report["mappings"]["not_available"][stat] = {
                "live_api_path": "N/A",
                "status": "not_available",
                "notes": "Não mapeado"
            }
    
    # Processa variáveis agregadas de time
    for col in categories["team_aggregates"] + categories["derived"]:
        # Remove prefixo blue_/red_ para verificar
        base_name = col
        if col in team_var_mappings:
            mapping = team_var_mappings[col]
            report["mappings"][mapping["status"]][col] = mapping
        else:
            report["mappings"]["not_available"][col] = {
                "live_api_path": "N/A",
                "status": "not_available",
                "notes": "Não mapeado"
            }
    
    # Calcula resumo
    total_vars = len(unique_player_stats) + len(categories["team_aggregates"]) + len(categories["derived"])
    report["summary"] = {
        "total_training_columns": len(training_columns),
        "total_unique_variables": total_vars,
        "available": len(report["mappings"]["available"]),
        "partial": len(report["mappings"]["partial"]),
        "calculable": len(report["mappings"]["calculable"]),
        "not_available": len(report["mappings"]["not_available"])
    }
    
    return report


# ============================================================================
# FUNÇÕES DE OUTPUT
# ============================================================================

def save_snapshot(data: Dict[str, Any], filename: str = None) -> str:
    """
    Salva os dados em um arquivo JSON.
    
    Args:
        data: Dados a serem salvos
        filename: Nome do arquivo (opcional, gera automático)
    
    Returns:
        Caminho do arquivo salvo
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    if filename is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"live_game_snapshot_{timestamp}.json"
    
    filepath = os.path.join(OUTPUT_DIR, filename)
    
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    print(f"[OK] Dados salvos em: {filepath}")
    return filepath


def save_variables_list(data: Dict[str, Any], filename: str = "live_game_variables.txt") -> str:
    """
    Salva a lista de variáveis disponíveis em um arquivo texto.
    
    Args:
        data: Dados da API
        filename: Nome do arquivo
    
    Returns:
        Caminho do arquivo salvo
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filepath = os.path.join(OUTPUT_DIR, filename)
    
    categories = categorize_live_api_vars(data)
    
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("VARIÁVEIS DISPONÍVEIS NA LIVE CLIENT DATA API\n")
        f.write(f"Gerado em: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 80 + "\n\n")
        
        for category, variables in categories.items():
            f.write(f"\n{'='*40}\n")
            f.write(f"{category.upper()} ({len(variables)} variáveis)\n")
            f.write(f"{'='*40}\n")
            for var in sorted(variables):
                f.write(f"  - {var}\n")
    
    print(f"[OK] Lista de variáveis salva em: {filepath}")
    return filepath


def print_comparison_report(report: Dict[str, Any]):
    """
    Imprime o relatório de comparação com o dataset de treinamento.
    """
    if "error" in report:
        print(f"\n[ERRO] {report['error']}")
        return
    
    print("\n" + "=" * 80)
    print("COMPARAÇÃO: LIVE CLIENT API vs DATASET DE TREINAMENTO")
    print("=" * 80)
    
    # Info do dataset
    info = report["training_data_info"]
    print(f"\n📊 DATASET DE TREINAMENTO (training_data_head.csv):")
    print(f"   Total de colunas: {info['total_columns']}")
    print(f"   Metadados: {info['metadata_columns']}")
    print(f"   Stats por jogador (p1-p10): {info['player_stat_columns']}")
    print(f"   Agregados por time: {info['team_aggregate_columns']}")
    print(f"   Features derivadas: {info['derived_columns']}")
    print(f"   Stats únicos de jogador: {info['unique_player_stats']}")
    
    # Resumo de mapeamentos
    summary = report["summary"]
    print(f"\n📈 RESUMO DOS MAPEAMENTOS:")
    print(f"   ✅ Disponíveis (todos jogadores): {summary['available']}")
    print(f"   ⚠️  Parciais (apenas activePlayer): {summary['partial']}")
    print(f"   🔧 Calculáveis: {summary['calculable']}")
    print(f"   ❌ Não disponíveis: {summary['not_available']}")
    
    mappings = report["mappings"]
    
    # Disponíveis
    if mappings["available"]:
        print(f"\n✅ DISPONÍVEIS PARA TODOS OS JOGADORES ({len(mappings['available'])}):")
        for var, info in mappings["available"].items():
            print(f"   {var:25} -> {info['live_api_path']}")
            if info.get("notes"):
                print(f"   {'':25}    ({info['notes']})")
    
    # Parciais
    if mappings["partial"]:
        print(f"\n⚠️  DISPONÍVEIS APENAS PARA JOGADOR LOCAL ({len(mappings['partial'])}):")
        for var, info in list(mappings["partial"].items())[:10]:  # Mostra só 10
            print(f"   {var:25} -> {info['live_api_path']}")
        if len(mappings["partial"]) > 10:
            print(f"   ... e mais {len(mappings['partial']) - 10} variáveis")
    
    # Calculáveis
    if mappings["calculable"]:
        print(f"\n🔧 CALCULÁVEIS A PARTIR DOS DADOS ({len(mappings['calculable'])}):")
        for var, info in list(mappings["calculable"].items())[:15]:  # Mostra só 15
            print(f"   {var:25} -> {info['live_api_path']}")
        if len(mappings["calculable"]) > 15:
            print(f"   ... e mais {len(mappings['calculable']) - 15} variáveis")
    
    # Não disponíveis
    if mappings["not_available"]:
        print(f"\n❌ NÃO DISPONÍVEIS NA LIVE API ({len(mappings['not_available'])}):")
        for var, info in list(mappings["not_available"].items())[:20]:  # Mostra só 20
            print(f"   - {var}")
        if len(mappings["not_available"]) > 20:
            print(f"   ... e mais {len(mappings['not_available']) - 20} variáveis")
    
    # Estatísticas únicas de jogador
    print(f"\n📋 STATS ÚNICOS DE JOGADOR ({len(report['unique_player_stats'])}):")
    stats = report['unique_player_stats']
    for i in range(0, len(stats), 5):
        row = stats[i:i+5]
        print(f"   {', '.join(row)}")
    
    print("\n" + "=" * 80)
    print("NOTAS IMPORTANTES:")
    print("  1. Champion stats detalhados só estão disponíveis para o jogador local")
    print("  2. Gold total e XP não são expostos pela Live Client API")
    print("  3. Stats de dano não estão disponíveis na Live API")
    print("  4. Muitas variáveis podem ser calculadas a partir de KDA e eventos")
    print("=" * 80)


def print_live_data_structure(data: Dict[str, Any]):
    """
    Imprime a estrutura dos dados da Live API de forma legível.
    """
    print("\n" + "=" * 80)
    print("ESTRUTURA DOS DADOS DA LIVE CLIENT API")
    print("=" * 80)
    
    # Active Player
    if "activePlayer" in data:
        print("\n🎮 ACTIVE PLAYER (Jogador Local):")
        active = data["activePlayer"]
        print(f"   Summoner Name: {active.get('riotIdGameName', 'N/A')}#{active.get('riotIdTagLine', '')}")
        print(f"   Champion: {active.get('rawChampionName', 'N/A')}")
        print(f"   Level: {active.get('level', 'N/A')}")
        if "championStats" in active:
            stats = active["championStats"]
            print(f"   HP: {stats.get('currentHealth', 0):.0f}/{stats.get('maxHealth', 0):.0f}")
            print(f"   AD: {stats.get('attackDamage', 0):.0f} | AP: {stats.get('abilityPower', 0):.0f}")
            print(f"   Armor: {stats.get('armor', 0):.0f} | MR: {stats.get('magicResist', 0):.0f}")
    
    # Game Data
    if "gameData" in data:
        print("\n⏱️  GAME DATA:")
        game = data["gameData"]
        game_time = game.get("gameTime", 0)
        minutes = int(game_time // 60)
        seconds = int(game_time % 60)
        print(f"   Tempo: {minutes}:{seconds:02d}")
        print(f"   Modo: {game.get('gameMode', 'N/A')}")
        print(f"   Map: {game.get('mapName', 'N/A')}")
    
    # All Players
    if "allPlayers" in data:
        print("\n👥 ALL PLAYERS:")
        blue_team = []
        red_team = []
        
        for player in data["allPlayers"]:
            team = player.get("team", "")
            name = f"{player.get('riotIdGameName', '?')}#{player.get('riotIdTagLine', '')}"
            champ = player.get("rawChampionName", "?").replace("game_character_displayname_", "")
            scores = player.get("scores", {})
            kda = f"{scores.get('kills', 0)}/{scores.get('deaths', 0)}/{scores.get('assists', 0)}"
            cs = scores.get("creepScore", 0)
            
            info = f"   {champ:15} | {name:25} | KDA: {kda:10} | CS: {cs}"
            
            if team == "ORDER":  # Blue team
                blue_team.append(info)
            else:  # Red team
                red_team.append(info)
        
        print("\n   🔵 BLUE TEAM (ORDER):")
        for p in blue_team:
            print(p)
        
        print("\n   🔴 RED TEAM (CHAOS):")
        for p in red_team:
            print(p)
    
    # Events
    if "events" in data and "Events" in data["events"]:
        events = data["events"]["Events"]
        print(f"\n📋 EVENTS ({len(events)} eventos):")
        # Mostra últimos 5 eventos
        for event in events[-5:]:
            event_name = event.get("EventName", "Unknown")
            event_time = event.get("EventTime", 0)
            minutes = int(event_time // 60)
            seconds = int(event_time % 60)
            print(f"   [{minutes}:{seconds:02d}] {event_name}")


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Coleta dados em tempo real de uma partida de League of Legends",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  python live_game_scraper.py              # Coleta e salva snapshot
  python live_game_scraper.py --compare    # Compara com variáveis do Match-V5
  python live_game_scraper.py --structure  # Mostra estrutura dos dados
  python live_game_scraper.py --all        # Faz tudo acima
        """
    )
    
    parser.add_argument(
        "--compare", "-c",
        action="store_true",
        help="Compara variáveis com as usadas no modelo Match-V5"
    )
    
    parser.add_argument(
        "--structure", "-s",
        action="store_true",
        help="Mostra a estrutura dos dados de forma legível"
    )
    
    parser.add_argument(
        "--all", "-a",
        action="store_true",
        help="Executa todas as opções (snapshot + compare + structure)"
    )
    
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Não salva os dados em arquivo"
    )
    
    args = parser.parse_args()
    
    # Se --all, ativa todas as opções
    if args.all:
        args.compare = True
        args.structure = True
    
    print("=" * 80)
    print("🎮 LEAGUE OF LEGENDS - LIVE CLIENT DATA SCRAPER")
    print("=" * 80)
    
    # Verifica se há partida ativa
    print("\n[INFO] Verificando conexão com o cliente do LoL...")
    
    if not check_game_active():
        print("\n" + "!" * 80)
        print("❌ NENHUMA PARTIDA ATIVA ENCONTRADA!")
        print("!" * 80)
        print("\nPara usar este script, você precisa:")
        print("  1. Abrir o League of Legends")
        print("  2. Entrar em uma partida (Normal, Ranked, ARAM, etc.)")
        print("  3. Executar este script durante a partida")
        print("\nNota: O script não funciona durante a tela de carregamento.")
        print("      Aguarde até estar dentro do jogo.")
        return 1
    
    print("[OK] Partida ativa detectada!")
    
    # Busca todos os dados
    print("\n[INFO] Coletando dados da partida...")
    data = get_live_data("all")
    
    if data is None:
        print("[ERRO] Falha ao coletar dados.")
        return 1
    
    # Mostra estrutura dos dados
    if args.structure or not (args.compare or args.no_save):
        print_live_data_structure(data)
    
    # Salva snapshot
    if not args.no_save:
        print("\n[INFO] Salvando dados...")
        save_snapshot(data, "live_game_snapshot.json")
        save_variables_list(data)
    
    # Compara com dataset de treinamento
    if args.compare:
        print("\n[INFO] Comparando com colunas do dataset de treinamento...")
        report = compare_with_training_data(data)
        print_comparison_report(report)
        
        # Salva relatório
        if not args.no_save:
            report_path = os.path.join(OUTPUT_DIR, "live_vs_training_comparison.json")
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
            print(f"\n[OK] Relatório salvo em: {report_path}")
    
    print("\n" + "=" * 80)
    print("✅ COLETA CONCLUÍDA COM SUCESSO!")
    print("=" * 80)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())

