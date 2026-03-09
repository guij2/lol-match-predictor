# Script para coletar dados de timeline para TODOS os minutos usando Match V5 API
# 
# Coleta TODAS as variáveis disponíveis para CADA MINUTO de jogo:
# - Análise de importância de variáveis ao longo do tempo
# - Previsão de vencedor em qualquer momento da partida
# - Análise de evolução temporal do jogo
#
# Otimizações implementadas:
# - ✅ SEM cache de timelines - elimina travamentos!
# - ✅ Salvamento incremental POR PARTIDA - pode parar a qualquer momento
# - ✅ Detecção automática de partidas já processadas via CSV - não reprocessa
# - ✅ Rate limiting inteligente por região (1.2s entre chamadas)
# - ✅ Prints detalhados e frequentes de progresso
# - ✅ Estatísticas em tempo real (velocidade, ETA)
# - ✅ Tratamento robusto de erros com backoff exponencial
# - ✅ Apenas 1 API call por partida (muito eficiente!)
# - ✅ Processamento vetorizado para máxima performance
# - ✅ Coleta de ouro gasto (spentGold) via eventos de itens + Data Dragon

import os
import json
import csv
import time
import random
from typing import Dict, Optional, List, Set
from urllib.parse import urlparse
from threading import Lock
from collections import defaultdict

import pandas as pd
import requests


# Chave da API Riot Games
api_key = os.environ.get("RIOT_API_KEY", "RGAPI-YOUR-KEY-HERE")

# =============================================================================
# CACHE DE PREÇOS DE ITENS (Data Dragon)
# =============================================================================
_item_prices: Dict[int, int] = {}
_item_sell_prices: Dict[int, int] = {}
_item_is_consumable: Dict[int, bool] = {}
_items_loaded = False


def load_item_prices() -> bool:
    """
    Carrega os preços de todos os itens da Data Dragon.
    Retorna True se carregou com sucesso, False caso contrário.
    
    Os preços são cacheados globalmente para evitar múltiplas requisições.
    """
    global _item_prices, _item_sell_prices, _item_is_consumable, _items_loaded
    
    if _items_loaded:
        return True
    
    print("[INFO] Carregando preços de itens da Data Dragon...", flush=True)
    
    try:
        # Primeiro, pega a versão mais recente do jogo
        versions_url = "https://ddragon.leagueoflegends.com/api/versions.json"
        response = requests.get(versions_url, timeout=10)
        if not response.ok:
            print(f"[ERROR] Erro ao buscar versões: {response.status_code}", flush=True)
            return False
        
        versions = response.json()
        latest_version = versions[0]
        print(f"[INFO] Versão do jogo: {latest_version}", flush=True)
        
        # Agora busca os dados dos itens
        items_url = f"https://ddragon.leagueoflegends.com/cdn/{latest_version}/data/en_US/item.json"
        response = requests.get(items_url, timeout=30)
        if not response.ok:
            print(f"[ERROR] Erro ao buscar itens: {response.status_code}", flush=True)
            return False
        
        items_data = response.json()
        items = items_data.get("data", {})
        
        # Extrai preços de cada item
        for item_id_str, item_info in items.items():
            try:
                item_id = int(item_id_str)
                gold_info = item_info.get("gold", {})
                
                # Preço total do item (para compra)
                total_price = gold_info.get("total", 0)
                _item_prices[item_id] = total_price
                
                # Preço de venda (geralmente 70% do total, mas alguns itens são diferentes)
                # Se "sell" não existir, usa 70% como fallback
                sell_price = gold_info.get("sell", int(total_price * 0.7))
                _item_sell_prices[item_id] = sell_price
                
                # Verifica se é consumível ou trinket (não deve ser subtraído no ITEM_DESTROYED)
                tags = item_info.get("tags", [])
                consumed = item_info.get("consumed", False)
                _item_is_consumable[item_id] = "Consumable" in tags or "Trinket" in tags or consumed
                
            except (ValueError, TypeError):
                continue
        
        _items_loaded = True
        print(f"[OK] {len(_item_prices)} itens carregados com preços", flush=True)
        
        # Alguns exemplos para debug
        examples = [(1054, "Doran's Shield"), (1055, "Doran's Blade"), (3006, "Berserker's Greaves")]
        for item_id, name in examples:
            price = _item_prices.get(item_id, 0)
            print(f"    Exemplo: {name} (ID {item_id}) = {price} gold", flush=True)
        
        return True
        
    except Exception as e:
        print(f"[ERROR] Exceção ao carregar preços de itens: {e}", flush=True)
        return False


def get_item_price(item_id: int) -> int:
    """Retorna o preço de compra de um item. Retorna 0 se não encontrado."""
    return _item_prices.get(item_id, 0)


def get_item_sell_price(item_id: int) -> int:
    """Retorna o preço de venda de um item. Retorna 0 se não encontrado."""
    return _item_sell_prices.get(item_id, 0)


def is_item_consumable(item_id: int) -> bool:
    """Retorna True se o item é consumível/trinket (pots, wards, etc)."""
    return _item_is_consumable.get(item_id, False)


# Configurações de throttling e backoff
# Rate limit: 20 req/s, 100 req/2min por região
_last_call_per_host: Dict[str, float] = {}
_sessions: Dict[str, requests.Session] = {}
_host_state_lock = Lock()
_MIN_INTERVAL_SECONDS = 1.2  # ~1 chamada a cada 1.2s por região


# Mapeamento de plataformas para hosts regionais
PLATFORM_TO_REGIONAL = {
    "BR1": "americas",
    "LA1": "americas", 
    "LA2": "americas",
    "NA1": "americas",
    "OC1": "americas",
    "EUW1": "europe",
    "EUN1": "europe",
    "TR1": "europe",
    "RU": "europe",
    "JP1": "asia",
    "KR": "asia",
}


def get_regional_host(platform: str) -> str:
    """Retorna o host regional da API"""
    regional = PLATFORM_TO_REGIONAL.get(platform, "americas")
    return f"https://{regional}.api.riotgames.com"


def get_json_with_backoff(url: str, max_retries: int = 3, initial_delay_seconds: float = 1.5, timeout_seconds: float = 10.0):
    """Faz requisições HTTP com backoff exponencial e rate limiting"""
    delay_seconds = initial_delay_seconds
    
    # Extrai informações do URL para prints informativos
    parsed_url = urlparse(url)
    host = parsed_url.netloc
    path = parsed_url.path
    
    # Identifica o tipo de endpoint
    endpoint_type = "TIMELINE"
    
    for attempt in range(max_retries):
        # Throttle por host
        with _host_state_lock:
            now = time.time()
            last = _last_call_per_host.get(host, 0.0)
            wait = (last + _MIN_INTERVAL_SECONDS) - now
        if wait > 0:
            time.sleep(wait)

        try:
            # Marca início e usa Session por host
            start_time = time.time()
            with _host_state_lock:
                _last_call_per_host[host] = time.time()
                session = _sessions.get(host)
                if session is None:
                    session = requests.Session()
                    _sessions[host] = session
            
            print(f"    [API] {endpoint_type} -> {host}{path[:60]}{'...' if len(path) > 60 else ''}", flush=True)
            response = session.get(url, timeout=timeout_seconds)
            elapsed = time.time() - start_time
            
        except (requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.RequestException) as e:
            print(f"    [ERROR] EXC {e.__class__.__name__} host={host}; backoff {delay_seconds:.2f}s", flush=True)
            time.sleep(delay_seconds + random.uniform(0, 0.25))
            delay_seconds = min(delay_seconds * 2, 10)
            continue

        if response.status_code == 429:
            retry_after_header = response.headers.get('Retry-After')
            if retry_after_header:
                try:
                    sleep_s = float(retry_after_header) + 0.1
                    print(f"    [WARN]  [429] Rate limited! Aguardando {sleep_s:.2f}s (Retry-After header)", flush=True)
                    time.sleep(sleep_s)
                except ValueError:
                    print(f"    [WARN]  [429] Rate limited! Backoff {delay_seconds:.2f}s", flush=True)
                    time.sleep(delay_seconds)
            else:
                print(f"    [WARN]  [429] Rate limited! Backoff {delay_seconds:.2f}s", flush=True)
                time.sleep(delay_seconds)
        elif 500 <= response.status_code < 600:
            print(f"    [WARN]  [{response.status_code}] Server error! Backoff {delay_seconds:.2f}s", flush=True)
            time.sleep(delay_seconds + random.uniform(0, 0.25))
        elif response.ok:
            try:
                data = response.json()
                print(f"    [OK] [200] {endpoint_type} OK ({elapsed:.2f}s)", flush=True)
                return data
            except json.JSONDecodeError:
                print(f"    [ERROR] [JSON] Decode error! Backoff {delay_seconds:.2f}s", flush=True)
                time.sleep(delay_seconds)
        else:
            # Outros erros (404, etc) - não retenta
            print(f"    [ERROR] [{response.status_code}] Erro HTTP - não retentando", flush=True)
            return None

        delay_seconds = min(delay_seconds * 2, 10)

    print(f"    [ERROR] [FAIL] Max retries ({max_retries}) atingido para {endpoint_type}", flush=True)
    return None


def get_match_timeline_full(match_id: str, region: str, stats: dict = None) -> Optional[dict]:
    """
    Busca os dados de timeline COMPLETA de uma partida usando Match V5
    
    Args:
        match_id: ID da partida (formato: REGION_matchId)
        region: Região da partida (BR1, NA1, etc)
        stats: Dicionário de estatísticas
    
    Returns:
        Dicionário com todos os frames da partida ou None se erro
    """
    # Busca da API
    regional_host = get_regional_host(region)
    url = f"{regional_host}/lol/match/v5/matches/{match_id}/timeline?api_key={api_key}"
    
    data = get_json_with_backoff(url)
    
    if not isinstance(data, dict):
        return None
    
    # Extrai todos os frames
    frames = data.get("info", {}).get("frames", [])
    if len(frames) < 2:  # Precisa de pelo menos 2 frames (0 e 1 minuto)
        print(f"    [WARN] Partida muito curta! Apenas {len(frames)} frames", flush=True)
        if stats:
            stats["too_short"] += 1
        return None
    
    # Retorna frames completos
    result = {
        "frames": frames,
        "num_frames": len(frames)
    }
    
    return result


def extract_features_for_all_frames(frames: list, match_id: str, region: str, winner_team: int) -> List[dict]:
    """
    Extrai features para TODOS os frames/minutos da partida
    Otimizado para performance - processa eventos uma vez e acumula
    
    Args:
        frames: Lista de frames da timeline
        match_id: ID da partida
        region: Região
        winner_team: Time vencedor (100 = blue, 200 = red)
    
    Returns:
        Lista de dicionários, um por frame, com todas as features
    """
    if not frames:
        print(f"    [ERROR] Frames list está vazia", flush=True)
        return []
    
    num_frames = len(frames)
    print(f"    [INFO] Processando {num_frames} frames (minutos) da partida", flush=True)
    
    all_features = []
    
    # Acumuladores para eventos (processamos uma vez e vamos somando)
    cumulative_events = {
        'blue_kills': 0, 'red_kills': 0,
        'blue_deaths': 0, 'red_deaths': 0,
        'blue_assists': 0, 'red_assists': 0,
        'blue_dragons': 0, 'red_dragons': 0,
        'blue_elder_dragons': 0, 'red_elder_dragons': 0,
        'blue_barons': 0, 'red_barons': 0,
        'blue_voidgrubs': 0, 'red_voidgrubs': 0,
        'blue_heralds': 0, 'red_heralds': 0,
        'blue_towers': 0, 'red_towers': 0,
        'blue_inhibitors': 0, 'red_inhibitors': 0,
        'blue_elite_monsters': 0, 'red_elite_monsters': 0
    }
    
    # Acumuladores para ouro gasto por jogador (p1-p10)
    # Rastreia o custo total dos itens comprados menos vendas/undos
    spent_gold_per_player = {i: 0 for i in range(1, 11)}
    
    # Processa cada frame
    for frame_idx, frame in enumerate(frames):
        timestamp = frame.get("timestamp", frame_idx * 60000)
        time_minutes = frame_idx  # Frame index = minutos
        
        participant_frames = frame.get("participantFrames", {})
        frame_events = frame.get("events", [])
        
        # Atualiza eventos cumulativos deste frame
        for event in frame_events:
            event_type = event.get("type", "")
            
            # Champion kills
            if event_type == "CHAMPION_KILL":
                killer_id = event.get("killerId", 0)
                assisting_ids = event.get("assistingParticipantIds", [])
                
                if 1 <= killer_id <= 5:  # Blue team
                    cumulative_events['blue_kills'] += 1
                    cumulative_events['red_deaths'] += 1
                    cumulative_events['blue_assists'] += len([a for a in assisting_ids if 1 <= a <= 5])
                elif 6 <= killer_id <= 10:  # Red team
                    cumulative_events['red_kills'] += 1
                    cumulative_events['blue_deaths'] += 1
                    cumulative_events['red_assists'] += len([a for a in assisting_ids if 6 <= a <= 10])
            
            # Building destroy
            elif event_type == "BUILDING_KILL":
                killer_team_id = event.get("killerTeamId", 0)
                building_type = event.get("buildingType", "")
                
                if building_type == "TOWER_BUILDING":
                    if killer_team_id == 100:
                        cumulative_events['blue_towers'] += 1
                    elif killer_team_id == 200:
                        cumulative_events['red_towers'] += 1
                elif building_type == "INHIBITOR_BUILDING":
                    if killer_team_id == 100:
                        cumulative_events['blue_inhibitors'] += 1
                    elif killer_team_id == 200:
                        cumulative_events['red_inhibitors'] += 1
            
            # Elite monster kills
            elif event_type == "ELITE_MONSTER_KILL":
                killer_team_id = event.get("killerTeamId", 0)
                monster_type = event.get("monsterType", "")
                monster_sub_type = event.get("monsterSubType", "")
                
                # Dragões (regular e Elder)
                if monster_type == "DRAGON":
                    if monster_sub_type == "ELDER_DRAGON":
                        # Dragão Ancião
                        if killer_team_id == 100:
                            cumulative_events['blue_elder_dragons'] += 1
                        elif killer_team_id == 200:
                            cumulative_events['red_elder_dragons'] += 1
                    else:
                        # Dragões elementais regulares
                        if killer_team_id == 100:
                            cumulative_events['blue_dragons'] += 1
                        elif killer_team_id == 200:
                            cumulative_events['red_dragons'] += 1
                
                # Arauto do Vale
                elif monster_type == "RIFTHERALD":
                    if killer_team_id == 100:
                        cumulative_events['blue_heralds'] += 1
                    elif killer_team_id == 200:
                        cumulative_events['red_heralds'] += 1
                
                # Barão Nashor
                elif monster_type == "BARON_NASHOR":
                    if killer_team_id == 100:
                        cumulative_events['blue_barons'] += 1
                    elif killer_team_id == 200:
                        cumulative_events['red_barons'] += 1
                
                # Vastilarvas (Voidgrubs)
                elif monster_type == "HORDE":
                    if killer_team_id == 100:
                        cumulative_events['blue_voidgrubs'] += 1
                    elif killer_team_id == 200:
                        cumulative_events['red_voidgrubs'] += 1
                
                # Conta todos elite monsters
                if killer_team_id == 100:
                    cumulative_events['blue_elite_monsters'] += 1
                elif killer_team_id == 200:
                    cumulative_events['red_elite_monsters'] += 1
            
            # Item events - rastrear ouro gasto
            elif event_type == "ITEM_PURCHASED":
                participant_id = event.get("participantId", 0)
                item_id = event.get("itemId", 0)
                if 1 <= participant_id <= 10 and item_id > 0:
                    item_price = get_item_price(item_id)
                    spent_gold_per_player[participant_id] += item_price
            
            elif event_type == "ITEM_SOLD":
                participant_id = event.get("participantId", 0)
                item_id = event.get("itemId", 0)
                if 1 <= participant_id <= 10 and item_id > 0:
                    # Ao vender, o jogador recupera parte do ouro
                    sell_price = get_item_sell_price(item_id)
                    spent_gold_per_player[participant_id] -= sell_price
            
            elif event_type == "ITEM_DESTROYED":
                # Componentes são destruídos em upgrades - subtrai para evitar contagem dupla.
                # Ex: Compra Long Sword (350g), upgrade para BF Sword (1300g total):
                #   ITEM_PURCHASED(Long Sword) +350, ITEM_DESTROYED(Long Sword) -350,
                #   ITEM_PURCHASED(BF Sword) +1300 → net = 1300g (correto)
                # Exceção: consumíveis (pots, wards) são destruídos ao usar, mas o ouro foi gasto.
                participant_id = event.get("participantId", 0)
                item_id = event.get("itemId", 0)
                if 1 <= participant_id <= 10 and item_id > 0:
                    if not is_item_consumable(item_id):
                        spent_gold_per_player[participant_id] -= get_item_price(item_id)
            
            elif event_type == "ITEM_UNDO":
                participant_id = event.get("participantId", 0)
                before_id = event.get("beforeId", 0)  # Item que tinha antes do undo
                after_id = event.get("afterId", 0)    # Item que ficou após o undo
                if 1 <= participant_id <= 10:
                    # Undo reverte a compra: remove preço do item comprado
                    if before_id > 0:
                        spent_gold_per_player[participant_id] -= get_item_price(before_id)
                    # Se tinha um item antes (upgrade), adiciona de volta
                    if after_id > 0:
                        spent_gold_per_player[participant_id] += get_item_price(after_id)
        
        # Extrai features para este frame
        features = extract_features_for_single_frame(
            participant_frames, cumulative_events, spent_gold_per_player, match_id, region, 
            winner_team, time_minutes, timestamp
        )
        
        if features:
            all_features.append(features)
    
    print(f"    [OK] {len(all_features)} frames processados com sucesso", flush=True)
    return all_features


def extract_features_for_single_frame(participant_frames: dict, cumulative_events: dict, 
                                      spent_gold_per_player: dict,
                                      match_id: str, region: str, winner_team: int,
                                      time_minutes: int, timestamp: int) -> Optional[dict]:
    """
    Extrai features de um único frame - OTIMIZADO para velocidade
    
    Args:
        participant_frames: Dados dos participantes neste frame
        cumulative_events: Eventos acumulados até este frame
        spent_gold_per_player: Ouro gasto por cada jogador (p1-p10)
        match_id: ID da partida
        region: Região
        winner_team: Time vencedor
        time_minutes: Minutos decorridos
        timestamp: Timestamp em ms
    
    Returns:
        Dicionário com features do frame
    """
    
    # Inicializa features com informações básicas
    features = {
        "matchId": match_id,
        "Region": region,
        "blueWin": 1 if winner_team == 100 else 0,
        "time": time_minutes,  # Minutos decorridos
        "timestamp": timestamp  # Timestamp em ms
    }
    
    # === FEATURES POR JOGADOR (p1-p10) ===
    # p1-p5 = Blue Team (100), p6-p10 = Red Team (200)
    for participant_id in range(1, 11):
        participant_key = str(participant_id)
        
        if participant_key not in participant_frames:
            print(f"    [WARN] Participante {participant_id} não encontrado nos frames", flush=True)
            continue
        
        pf = participant_frames[participant_key]
        
        # Estatísticas básicas
        features[f"p{participant_id}_level"] = pf.get("level", 0)
        features[f"p{participant_id}_totalGold"] = pf.get("totalGold", 0)
        features[f"p{participant_id}_currentGold"] = pf.get("currentGold", 0)
        features[f"p{participant_id}_xp"] = pf.get("xp", 0)
        
        # Ouro gasto (soma do valor dos itens no inventário)
        features[f"p{participant_id}_spentGold"] = spent_gold_per_player.get(participant_id, 0)
        
        # Minions e jungle
        features[f"p{participant_id}_minionsKilled"] = pf.get("minionsKilled", 0)
        features[f"p{participant_id}_jungleMinionsKilled"] = pf.get("jungleMinionsKilled", 0)
        
        # CS total
        cs_total = pf.get("minionsKilled", 0) + pf.get("jungleMinionsKilled", 0)
        features[f"p{participant_id}_totalCS"] = cs_total
        
        # Jungle stats (se disponível)
        jungle_stats = pf.get("jungleMinionsKilled", 0)
        features[f"p{participant_id}_jungleCS"] = jungle_stats
        
        # Posição no mapa
        position = pf.get("position", {})
        features[f"p{participant_id}_posX"] = position.get("x", 0)
        features[f"p{participant_id}_posY"] = position.get("y", 0)
        
        # Dano stats
        damage_stats = pf.get("damageStats", {})
        features[f"p{participant_id}_magicDamageDone"] = damage_stats.get("magicDamageDone", 0)
        features[f"p{participant_id}_physicalDamageDone"] = damage_stats.get("physicalDamageDone", 0)
        features[f"p{participant_id}_trueDamageDone"] = damage_stats.get("trueDamageDone", 0)
        features[f"p{participant_id}_totalDamageDone"] = damage_stats.get("totalDamageDone", 0)
        
        features[f"p{participant_id}_magicDamageTaken"] = damage_stats.get("magicDamageDoneTpChampions", 0)
        features[f"p{participant_id}_physicalDamageTaken"] = damage_stats.get("physicalDamageDoneToChampions", 0)
        features[f"p{participant_id}_trueDamageTaken"] = damage_stats.get("trueDamageDoneToChampions", 0)
        features[f"p{participant_id}_totalDamageTaken"] = damage_stats.get("totalDamageDoneToChampions", 0)
        
        # Champion stats
        champion_stats = pf.get("championStats", {})
        features[f"p{participant_id}_abilityHaste"] = champion_stats.get("abilityHaste", 0)
        features[f"p{participant_id}_abilityPower"] = champion_stats.get("abilityPower", 0)
        features[f"p{participant_id}_armor"] = champion_stats.get("armor", 0)
        features[f"p{participant_id}_armorPen"] = champion_stats.get("armorPen", 0)
        features[f"p{participant_id}_armorPenPercent"] = champion_stats.get("armorPenPercent", 0)
        features[f"p{participant_id}_attackDamage"] = champion_stats.get("attackDamage", 0)
        features[f"p{participant_id}_attackSpeed"] = champion_stats.get("attackSpeed", 0)
        features[f"p{participant_id}_bonusArmorPenPercent"] = champion_stats.get("bonusArmorPenPercent", 0)
        features[f"p{participant_id}_bonusMagicPenPercent"] = champion_stats.get("bonusMagicPenPercent", 0)
        features[f"p{participant_id}_ccReduction"] = champion_stats.get("ccReduction", 0)
        features[f"p{participant_id}_cooldownReduction"] = champion_stats.get("cooldownReduction", 0)
        features[f"p{participant_id}_health"] = champion_stats.get("health", 0)
        features[f"p{participant_id}_healthMax"] = champion_stats.get("healthMax", 0)
        features[f"p{participant_id}_healthRegen"] = champion_stats.get("healthRegen", 0)
        features[f"p{participant_id}_lifesteal"] = champion_stats.get("lifesteal", 0)
        features[f"p{participant_id}_magicPen"] = champion_stats.get("magicPen", 0)
        features[f"p{participant_id}_magicPenPercent"] = champion_stats.get("magicPenPercent", 0)
        features[f"p{participant_id}_magicResist"] = champion_stats.get("magicResist", 0)
        features[f"p{participant_id}_movementSpeed"] = champion_stats.get("movementSpeed", 0)
        features[f"p{participant_id}_omnivamp"] = champion_stats.get("omnivamp", 0)
        features[f"p{participant_id}_physicalVamp"] = champion_stats.get("physicalVamp", 0)
        features[f"p{participant_id}_power"] = champion_stats.get("power", 0)
        features[f"p{participant_id}_powerMax"] = champion_stats.get("powerMax", 0)
        features[f"p{participant_id}_powerRegen"] = champion_stats.get("powerRegen", 0)
        features[f"p{participant_id}_spellVamp"] = champion_stats.get("spellVamp", 0)
    
    # === FEATURES AGREGADAS POR TIME ===
    # Blue Team (participantes 1-5)
    blue_team_indices = [str(i) for i in range(1, 6)]
    red_team_indices = [str(i) for i in range(6, 11)]
    
    # Totais de Gold
    blue_total_gold = sum(participant_frames.get(p, {}).get("totalGold", 0) for p in blue_team_indices)
    red_total_gold = sum(participant_frames.get(p, {}).get("totalGold", 0) for p in red_team_indices)
    features["blue_totalGold"] = blue_total_gold
    features["red_totalGold"] = red_total_gold
    features["goldDiff"] = blue_total_gold - red_total_gold
    
    # Totais de Ouro Gasto (spentGold) - soma do valor dos itens comprados
    blue_spent_gold = sum(spent_gold_per_player.get(i, 0) for i in range(1, 6))
    red_spent_gold = sum(spent_gold_per_player.get(i, 0) for i in range(6, 11))
    features["blue_spentGold"] = blue_spent_gold
    features["red_spentGold"] = red_spent_gold
    features["spentGoldDiff"] = blue_spent_gold - red_spent_gold
    
    # Totais de XP
    blue_total_xp = sum(participant_frames.get(p, {}).get("xp", 0) for p in blue_team_indices)
    red_total_xp = sum(participant_frames.get(p, {}).get("xp", 0) for p in red_team_indices)
    features["blue_totalXP"] = blue_total_xp
    features["red_totalXP"] = red_total_xp
    features["xpDiff"] = blue_total_xp - red_total_xp
    
    # Níveis médios
    blue_avg_level = sum(participant_frames.get(p, {}).get("level", 0) for p in blue_team_indices) / 5
    red_avg_level = sum(participant_frames.get(p, {}).get("level", 0) for p in red_team_indices) / 5
    features["blue_avgLevel"] = blue_avg_level
    features["red_avgLevel"] = red_avg_level
    features["levelDiff"] = blue_avg_level - red_avg_level
    
    # CS totais
    blue_total_cs = sum(
        participant_frames.get(p, {}).get("minionsKilled", 0) + 
        participant_frames.get(p, {}).get("jungleMinionsKilled", 0) 
        for p in blue_team_indices
    )
    red_total_cs = sum(
        participant_frames.get(p, {}).get("minionsKilled", 0) + 
        participant_frames.get(p, {}).get("jungleMinionsKilled", 0) 
        for p in red_team_indices
    )
    features["blue_totalCS"] = blue_total_cs
    features["red_totalCS"] = red_total_cs
    features["csDiff"] = blue_total_cs - red_total_cs
    
    # === EVENTOS CUMULATIVOS ATÉ ESTE MINUTO ===
    # Usa eventos já processados (passados como parâmetro)
    features["blue_kills"] = cumulative_events['blue_kills']
    features["red_kills"] = cumulative_events['red_kills']
    features["killDiff"] = cumulative_events['blue_kills'] - cumulative_events['red_kills']
    
    features["blue_deaths"] = cumulative_events['blue_deaths']
    features["red_deaths"] = cumulative_events['red_deaths']
    
    features["blue_assists"] = cumulative_events['blue_assists']
    features["red_assists"] = cumulative_events['red_assists']
    
    features["blue_dragons"] = cumulative_events['blue_dragons']
    features["red_dragons"] = cumulative_events['red_dragons']
    features["dragonDiff"] = cumulative_events['blue_dragons'] - cumulative_events['red_dragons']
    
    features["blue_elderDragons"] = cumulative_events['blue_elder_dragons']
    features["red_elderDragons"] = cumulative_events['red_elder_dragons']
    features["elderDragonDiff"] = cumulative_events['blue_elder_dragons'] - cumulative_events['red_elder_dragons']
    
    features["blue_barons"] = cumulative_events['blue_barons']
    features["red_barons"] = cumulative_events['red_barons']
    features["baronDiff"] = cumulative_events['blue_barons'] - cumulative_events['red_barons']
    
    features["blue_voidgrubs"] = cumulative_events['blue_voidgrubs']
    features["red_voidgrubs"] = cumulative_events['red_voidgrubs']
    features["voidgrubDiff"] = cumulative_events['blue_voidgrubs'] - cumulative_events['red_voidgrubs']
    
    features["blue_heralds"] = cumulative_events['blue_heralds']
    features["red_heralds"] = cumulative_events['red_heralds']
    features["heraldDiff"] = cumulative_events['blue_heralds'] - cumulative_events['red_heralds']
    
    features["blue_towers"] = cumulative_events['blue_towers']
    features["red_towers"] = cumulative_events['red_towers']
    features["towerDiff"] = cumulative_events['blue_towers'] - cumulative_events['red_towers']
    
    features["blue_inhibitors"] = cumulative_events['blue_inhibitors']
    features["red_inhibitors"] = cumulative_events['red_inhibitors']
    
    features["blue_eliteMonsters"] = cumulative_events['blue_elite_monsters']
    features["red_eliteMonsters"] = cumulative_events['red_elite_monsters']
    features["eliteMonsterDiff"] = cumulative_events['blue_elite_monsters'] - cumulative_events['red_elite_monsters']
    
    # === FEATURES DERIVADAS (ratios, vantagens) ===
    # Gold efficiency (gold per minute) - evita divisão por zero
    time_divisor = max(time_minutes, 1)
    features["blue_goldPerMin"] = blue_total_gold / time_divisor
    features["red_goldPerMin"] = red_total_gold / time_divisor
    
    # CS per minute
    features["blue_csPerMin"] = blue_total_cs / time_divisor
    features["red_csPerMin"] = red_total_cs / time_divisor
    
    # XP per minute
    features["blue_xpPerMin"] = blue_total_xp / time_divisor
    features["red_xpPerMin"] = red_total_xp / time_divisor
    
    # Kill/Death ratios
    features["blue_kd_ratio"] = cumulative_events['blue_kills'] / max(cumulative_events['blue_deaths'], 1)
    features["red_kd_ratio"] = cumulative_events['red_kills'] / max(cumulative_events['red_deaths'], 1)
    
    # Objective score (weighted sum)
    # Baron = 5pts, Elder Dragon = 4pts, Heralds = 3pts, Dragons = 2pts, Voidgrubs = 1pt, Towers = 1pt
    blue_obj_score = (
        (cumulative_events['blue_barons'] * 5) + 
        (cumulative_events['blue_elder_dragons'] * 4) + 
        (cumulative_events['blue_heralds'] * 3) + 
        (cumulative_events['blue_dragons'] * 2) + 
        (cumulative_events['blue_voidgrubs'] * 1) + 
        (cumulative_events['blue_towers'] * 1)
    )
    red_obj_score = (
        (cumulative_events['red_barons'] * 5) + 
        (cumulative_events['red_elder_dragons'] * 4) + 
        (cumulative_events['red_heralds'] * 3) + 
        (cumulative_events['red_dragons'] * 2) + 
        (cumulative_events['red_voidgrubs'] * 1) + 
        (cumulative_events['red_towers'] * 1)
    )
    features["blue_objectiveScore"] = blue_obj_score
    features["red_objectiveScore"] = red_obj_score
    features["objectiveScoreDiff"] = blue_obj_score - red_obj_score
    
    return features


def _get_matchid_index_path(output_csv: str) -> str:
    """Retorna o caminho do arquivo auxiliar de índice de matchIds processados."""
    base, _ = os.path.splitext(output_csv)
    return f"{base}_processed_matchids.txt"


def _append_matchid_to_index(output_csv: str, match_id: str):
    """Append de um matchId no índice auxiliar (uma linha por matchId)."""
    index_path = _get_matchid_index_path(output_csv)
    with open(index_path, "a", encoding="utf-8") as f:
        f.write(f"{match_id}\n")


def _rebuild_index_with_polars(output_csv: str) -> Optional[Set[str]]:
    """
    Reconstrói o índice de matchIds usando Polars (leitor CSV em Rust, muito rápido).
    Retorna None se polars não estiver disponível ou falhar.
    """
    try:
        import polars as pl
    except ImportError:
        return None

    try:
        print("[INFO] Usando Polars (Rust) para ler coluna matchId — rápido mesmo em CSV gigante...", flush=True)
        t0 = time.time()

        # scan_csv é lazy: só materializa a coluna pedida
        df = (
            pl.scan_csv(output_csv, low_memory=True)
            .select("matchId")
            .collect()
        )
        processed = set(df["matchId"].unique().to_list())

        elapsed = time.time() - t0
        print(f"[OK] Polars leu {len(df):,} linhas → {len(processed):,} matchIds únicos em {elapsed:.1f}s", flush=True)
        return processed

    except Exception as e:
        print(f"[WARN] Polars falhou ({e}), usando fallback streaming...", flush=True)
        return None


def _rebuild_index_streaming(output_csv: str, file_size: int) -> Set[str]:
    """
    Fallback: reconstrói índice lendo o CSV linha a linha em Python puro.
    Só usado se Polars não estiver disponível.
    """
    print(f"[INFO] Fallback streaming — lendo {file_size / (1024**3):.2f} GB linha a linha...", flush=True)
    processed = set()
    total_rows_scanned = 0
    log_every_rows = 500_000

    try:
        csv.field_size_limit(1024 * 1024 * 1024)
    except Exception:
        pass

    with open(output_csv, "r", encoding="utf-8", errors="replace") as f:
        header_line = f.readline()
        if not header_line:
            return processed

        header = next(csv.reader([header_line]))
        if "matchId" not in header:
            print("[WARN] Coluna 'matchId' não encontrada no CSV de saída.", flush=True)
            return processed

        matchid_idx = header.index("matchId")

        if matchid_idx == 0:
            # Caminho rápido: matchId é a primeira coluna — usa split mínimo
            for line in f:
                total_rows_scanned += 1
                match_id = line.split(",", 1)[0].strip().strip('"')
                if match_id:
                    processed.add(match_id)
                # Heartbeat a cada N linhas (sem time.time() por linha)
                if total_rows_scanned % log_every_rows == 0:
                    print(f"[INFO] Linhas: {total_rows_scanned:,} | MatchIds únicos: {len(processed):,}", flush=True)
        else:
            reader = csv.reader(f)
            for row in reader:
                total_rows_scanned += 1
                if matchid_idx < len(row):
                    match_id = row[matchid_idx].strip()
                    if match_id:
                        processed.add(match_id)
                if total_rows_scanned % log_every_rows == 0:
                    print(f"[INFO] Linhas: {total_rows_scanned:,} | MatchIds únicos: {len(processed):,}", flush=True)

    print(f"[OK] Streaming concluído: {total_rows_scanned:,} linhas → {len(processed):,} matchIds únicos", flush=True)
    return processed


def load_processed_match_ids(output_csv: str, file_size: int) -> Set[str]:
    """
    Carrega matchIds já processados de forma eficiente.

    Estratégia (em ordem de prioridade):
    1) Índice auxiliar (.txt) — leitura instantânea
    2) Polars scan_csv — lê só a coluna matchId em Rust, ~10-30s para 5 GB
    3) Fallback streaming — Python puro, mais lento mas sempre funciona

    Após (2) ou (3), salva o índice auxiliar para que próximas execuções usem (1).
    """
    index_path = _get_matchid_index_path(output_csv)

    # 1) Caminho instantâneo: índice auxiliar já existe
    if os.path.exists(index_path):
        print(f"[INFO] Carregando índice auxiliar: {index_path}", flush=True)
        processed = set()
        with open(index_path, "r", encoding="utf-8") as f:
            for line in f:
                mid = line.strip()
                if mid:
                    processed.add(mid)
        print(f"[OK] {len(processed):,} matchIds carregados do índice (instantâneo)", flush=True)
        return processed

    # 2) Tenta Polars (rápido, ~ segundos)
    processed = _rebuild_index_with_polars(output_csv)

    # 3) Fallback streaming se Polars não disponível
    if processed is None:
        processed = _rebuild_index_streaming(output_csv, file_size)

    # Salva índice auxiliar para próximas execuções
    if processed:
        with open(index_path, "w", encoding="utf-8") as f:
            for match_id in processed:
                f.write(f"{match_id}\n")
        print(f"[OK] Índice auxiliar salvo: {index_path} ({len(processed):,} matchIds)", flush=True)

    return processed


def process_matches_timeline_all_minutes(input_csv: str, output_csv: str, start_row: int = 0, max_rows: Optional[int] = None):
    """
    Processa o CSV de partidas e coleta dados de timeline para TODOS OS MINUTOS
    VERSÃO OTIMIZADA com salvamento incremental e prints detalhados
    
    Args:
        input_csv: Arquivo CSV de entrada (MatchChampionSelect.csv)
        output_csv: Arquivo CSV de saída com dados de todos os minutos
        start_row: Linha inicial para processar
        max_rows: Número máximo de linhas para processar (None = todas)
    
    NOTA: Cache foi REMOVIDO. Partidas já processadas são detectadas pelo CSV de saída.
          Isso elimina travamentos no salvamento do cache.
    """
    
    print("="*80, flush=True)
    print("COLETA DE DADOS DE TIMELINE - TODOS OS MINUTOS - LEAGUE OF LEGENDS", flush=True)
    print("="*80, flush=True)
    print("[INFO] Cache desabilitado - partidas já processadas detectadas via CSV", flush=True)
    
    # Carrega preços de itens da Data Dragon (necessário para calcular spentGold)
    if not load_item_prices():
        print("[WARN] Não foi possível carregar preços de itens. spentGold será 0.", flush=True)
    
    # Locks para thread-safety
    csv_write_lock = Lock()
    stats_lock = Lock()
    
    # Estatísticas globais
    stats = {
        "processed": 0,
        "skipped": 0,
        "errors": 0,
        "too_short": 0,
        "start_time": time.time(),
        "regional": defaultdict(lambda: {"processed": 0})
    }
    
    # Carrega CSV
    print(f"\n[INFO] Carregando CSV: {input_csv}", flush=True)
    df = pd.read_csv(input_csv)
    total_rows = len(df)
    print(f"[OK] {total_rows} partidas encontradas no CSV", flush=True)
    
    # Determina quais linhas processar
    end_row = min(start_row + max_rows, total_rows) if max_rows else total_rows
    df_to_process = df.iloc[start_row:end_row].copy()
    
    print(f"[OK] Range selecionado: linhas {start_row} a {end_row-1} ({len(df_to_process)} partidas)", flush=True)
    
    # Verifica/cria arquivo de saída e detecta partidas já processadas
    output_exists = os.path.exists(output_csv)
    processed_match_ids = set()
    print(f"\n[INFO] Verificando arquivo de saída: {output_csv}", flush=True)
    
    if output_exists:
        # Verifica se o arquivo não está vazio
        file_size = os.path.getsize(output_csv)
        
        if file_size == 0:
            print(f"[WARN] ⚠️ Arquivo de saída existe mas está VAZIO (0 bytes)!", flush=True)
            print(f"[INFO] Isso pode ter acontecido por:", flush=True)
            print(f"  1. Todas as partidas anteriores falharam", flush=True)
            print(f"  2. Interrupção durante criação do arquivo", flush=True)
            print(f"  3. Erro de escrita no disco", flush=True)
            print(f"[INFO] Recriando arquivo do zero...", flush=True)
            # Marca como não existe para recriar
            output_exists = False
        else:
            # Carrega partidas já processadas (OTIMIZADO - apenas coluna matchId)
            try:
                # Carrega com índice auxiliar; se não existir, reconstrói em chunks com progresso
                processed_match_ids = load_processed_match_ids(output_csv, file_size)
                print(f"[OK] {len(processed_match_ids)} partidas ÚNICAS já processadas", flush=True)
                
                # Debug: mostra algumas partidas já processadas
                if len(processed_match_ids) > 0:
                    sample = list(processed_match_ids)[:5]
                    print(f"[DEBUG] Exemplos de partidas já processadas: {sample}", flush=True)
                
                # Remove partidas já processadas do DataFrame a processar
                before_filter = len(df_to_process)
                df_to_process = df_to_process[~df_to_process["matchId"].isin(processed_match_ids)]
                after_filter = len(df_to_process)
                
                print(f"[INFO] Filtradas {before_filter - after_filter} partidas já processadas", flush=True)
                
                with stats_lock:
                    stats["skipped"] = len(processed_match_ids)
                
                if len(df_to_process) == 0:
                    print("[OK] ✅ Todas as partidas do range já foram processadas!", flush=True)
                    return
                
                print(f"[OK] ✅ {len(df_to_process)} partidas restantes para processar", flush=True)
                
                # Debug: mostra algumas que serão processadas
                if len(df_to_process) > 0:
                    sample_to_process = df_to_process["matchId"].head(3).tolist()
                    print(f"[DEBUG] Próximas a processar: {sample_to_process}", flush=True)
                    
            except pd.errors.EmptyDataError:
                print(f"[WARN] ⚠️ Arquivo existe mas não tem dados (apenas header vazio ou corrompido)!", flush=True)
                print(f"[INFO] Recriando arquivo do zero...", flush=True)
                # Marca como não existe para recriar
                output_exists = False
                
            except Exception as e:
                print(f"[ERROR] Erro ao verificar partidas processadas: {e}", flush=True)
                import traceback
                traceback.print_exc()
                print(f"[WARN] Continuando sem filtrar partidas já processadas...", flush=True)
    else:
        print(f"[INFO] ✨ Criando novo arquivo de saída: {output_csv}", flush=True)
    
    # Processa cada partida
    print(f"\n[START] Iniciando processamento...\n", flush=True)
    
    for idx, row in df_to_process.iterrows():
        match_id = row["matchId"]
        region = row["Region"]
        winner_team = 100 if row["blueWin"] == 1 else 200
        
        print(f"\n{'='*60}", flush=True)
        print(f"[MATCH] Processando: {match_id} (Região: {region})", flush=True)
        print(f"{'='*60}", flush=True)
        
        try:
            # Busca timeline completa
            print(f"  [INFO] Buscando timeline completa da partida...", flush=True)
            
            # Guarda contadores antes para detectar se foi "too_short"
            too_short_before = stats["too_short"]
            
            timeline_data = get_match_timeline_full(match_id, region, stats)
            
            if timeline_data is None:
                # Verifica se foi partida muito curta
                if stats["too_short"] > too_short_before:
                    print(f"  [SKIP] Partida muito curta (< 2 frames) - pulando", flush=True)
                else:
                    print(f"  [ERROR] Erro ao buscar timeline da partida", flush=True)
                    with stats_lock:
                        stats["errors"] += 1
                continue
            
            num_frames = timeline_data.get("num_frames", 0)
            print(f"  [OK] Timeline obtida: {num_frames} frames (minutos)", flush=True)
            
            # Extrai features para TODOS os frames
            print(f"  [INFO] Extraindo features para todos os {num_frames} minutos...", flush=True)
            frames = timeline_data.get("frames", [])
            all_features = extract_features_for_all_frames(frames, match_id, region, winner_team)
            
            if not all_features:
                print(f"  [ERROR] Erro ao extrair features da timeline", flush=True)
                with stats_lock:
                    stats["errors"] += 1
                continue
            
            print(f"  [OK] {len(all_features)} frames extraídos com {len(all_features[0])} variáveis cada", flush=True)
            
            # Salva IMEDIATAMENTE todas as linhas desta partida (salvamento incremental)
            with csv_write_lock:
                try:
                    df_match = pd.DataFrame(all_features)
                    
                    # Salva com ou sem header dependendo se arquivo existe
                    mode = 'a' if output_exists else 'w'
                    header = not output_exists
                    df_match.to_csv(output_csv, mode=mode, header=header, index=False)
                    _append_matchid_to_index(output_csv, str(match_id))
                    
                    print(f"    [SAVE] Partida {match_id} salva: {len(all_features)} linhas no CSV", flush=True)
                    
                    # Marca que arquivo agora existe
                    output_exists = True
                except Exception as e:
                    print(f"    [WARN] Erro ao salvar partida no CSV: {e}", flush=True)
            
            # Atualiza estatísticas
            with stats_lock:
                stats["processed"] += 1
                regional = PLATFORM_TO_REGIONAL.get(region, "unknown")
                stats["regional"][regional]["processed"] += 1
            
            # Imprime progresso detalhado
            with stats_lock:
                processed = stats["processed"]
                total = len(df_to_process)
                elapsed = time.time() - stats["start_time"]
                percent = (processed / total) * 100
                speed = processed / (elapsed / 60) if elapsed > 0 else 0
                eta_min = ((total - processed) / speed) if speed > 0 else 0
                
                print(f"\n[STATS] PROGRESSO: {processed}/{total} ({percent:.1f}%)", flush=True)
                print(f"[SPEED] Velocidade: {speed:.2f} partidas/min", flush=True)
                print(f"[ETA] Tempo estimado restante: {eta_min:.1f} minutos ({eta_min/60:.1f} horas)", flush=True)
                print(f"[ERRORS] Erros: {stats['errors']} | Partidas muito curtas: {stats['too_short']}", flush=True)
        
        except Exception as e:
            print(f"  [ERROR] Exceção não tratada: {e}", flush=True)
            import traceback
            traceback.print_exc()
            with stats_lock:
                stats["errors"] += 1
    
    # Resumo final
    print("\n" + "="*80, flush=True)
    print("✅ PROCESSAMENTO CONCLUÍDO!", flush=True)
    print("="*80, flush=True)
    
    total_time = time.time() - stats["start_time"]
    total_hours = total_time / 3600
    
    print(f"\n📊 RESUMO DE PROCESSAMENTO:", flush=True)
    print(f"  • Partidas processadas com sucesso: {stats['processed']}", flush=True)
    print(f"  • Partidas ignoradas (já processadas): {stats['skipped']}", flush=True)
    print(f"  • Partidas muito curtas (< 10 min): {stats['too_short']}", flush=True)
    print(f"  • Erros encontrados: {stats['errors']}", flush=True)
    
    print(f"\n⏱️  TEMPO E VELOCIDADE:", flush=True)
    if total_hours >= 1:
        print(f"  • Tempo total: {total_hours:.2f} horas ({total_time/60:.1f} minutos)", flush=True)
    else:
        print(f"  • Tempo total: {total_time/60:.1f} minutos", flush=True)
    
    if stats['processed'] > 0:
        avg_speed = stats['processed'] / (total_time / 60)
        print(f"  • Velocidade média: {avg_speed:.2f} partidas/min", flush=True)
        time_per_match = total_time / stats['processed']
        print(f"  • Tempo por partida: {time_per_match:.1f} segundos", flush=True)
    
    print(f"\n🗺️  ESTATÍSTICAS POR REGIÃO:", flush=True)
    for regional, reg_stats in sorted(stats["regional"].items()):
        print(f"  • {regional.upper()}: {reg_stats['processed']} partidas", flush=True)
    
    print(f"\n💾 ARQUIVO GERADO:", flush=True)
    print(f"  • Dados: {output_csv}", flush=True)
    
    print(f"\n💡 PARA CONTINUAR PROCESSANDO:", flush=True)
    print(f"  • Execute novamente o mesmo comando", flush=True)
    print(f"  • O script automaticamente detecta partidas já processadas no CSV", flush=True)
    print(f"  • Não há travamentos com salvamento de cache!", flush=True)
    
    print(f"\n✅ Coleta finalizada com sucesso!", flush=True)


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Coleta dados de timeline para TODOS OS MINUTOS usando Match V5 API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos de uso:
  
  # Processar primeiras 100 partidas
  python pythoncode/collect_10min_timeline.py --max-rows 100
  
  # Processar 1000 partidas
  python pythoncode/collect_10min_timeline.py --max-rows 1000
  
  # Continuar de onde parou (da linha 1000 em diante)
  python pythoncode/collect_10min_timeline.py --start 1000 --max-rows 1000
  
  # Processar todas as partidas
  python pythoncode/collect_10min_timeline.py
  
NOTA: Este script agora coleta dados de TODOS os minutos de cada partida,
      não apenas do minuto 10. Cada partida gerará múltiplas linhas no CSV
      (uma por minuto). A coluna 'time' indica quantos minutos decorreram.
        """
    )
    parser.add_argument("--input", default="MatchIds.csv", help="Arquivo CSV de entrada (colunas: matchId, Region, blueWin)")
    parser.add_argument("--output", default="D:/Data/MatchTimelineFull.csv", help="Arquivo CSV de saída")
    parser.add_argument("--start", type=int, default=0, help="Linha inicial para processar")
    parser.add_argument("--max-rows", type=int, default=None, help="Número máximo de linhas para processar")
    parser.add_argument(
        "--rebuild-index-only",
        action="store_true",
        help="Apenas reconstrói/carrega o índice de matchIds já processados do arquivo de saída e encerra",
    )
    
    args = parser.parse_args()

    if args.rebuild_index_only:
        if not os.path.exists(args.output):
            print(f"[ERROR] Arquivo de saída '{args.output}' não encontrado para indexação.", flush=True)
            return

        file_size = os.path.getsize(args.output)
        if file_size == 0:
            print(f"[ERROR] Arquivo de saída '{args.output}' está vazio.", flush=True)
            return

        print("[INFO] Reconstruindo/carregando índice de matchIds do arquivo existente...", flush=True)
        processed = load_processed_match_ids(args.output, file_size)
        print(f"[OK] Índice pronto com {len(processed)} matchIds únicos.", flush=True)
        return
    
    if not os.path.exists(args.input):
        print(f"[ERROR] ERRO: Arquivo de entrada '{args.input}' não encontrado!", flush=True)
        return

    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    
    process_matches_timeline_all_minutes(
        input_csv=args.input,
        output_csv=args.output,
        start_row=args.start,
        max_rows=args.max_rows
    )


if __name__ == "__main__":
    main()

