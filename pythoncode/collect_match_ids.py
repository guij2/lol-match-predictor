# Script para coletar Match IDs a partir de PUUIDs usando Match V5 API
#
# Etapa 2 do pipeline de coleta:
#   1. matchIDscraper.py       → puuid.csv (PUUIDs de jogadores ranked)
#   2. collect_match_ids.py    → D:/Data/MatchIds.csv (IDs de partidas + Region + blueWin)  ← ESTE SCRIPT
#   3. collect_10min_timeline.py → MatchTimelineAllMinutes.csv (dados de timeline)
#
# Para cada PUUID, busca as últimas N partidas ranqueadas (queue=420).
# Para cada partida única, busca os detalhes para extrair blueWin.
#
# Otimizações:
#   - Salvamento incremental por partida - pode parar e retomar
#   - Detecção automática de partidas já processadas
#   - Rate limiting inteligente por host regional (1.2s entre chamadas)
#   - 3 threads paralelas (uma por continente: americas/europe/asia)
#   - Backoff exponencial em erros 429/5xx
#
# Uso:
#   python pythoncode/collect_match_ids.py
#   python pythoncode/collect_match_ids.py --input puuid.csv --output D:/Data/MatchIds.csv --count 5

import os
import json
import time
import random
import argparse
from typing import Dict, List, Set
from urllib.parse import urlparse
from threading import Lock, Thread
from queue import Queue
import pandas as pd
import requests


# Chave da API Riot Games
api_key = os.environ.get("RIOT_API_KEY", "RGAPI-YOUR-KEY-HERE")

# =============================================================================
# RATE LIMITING E SESSÕES HTTP
# =============================================================================
_last_call_per_host: Dict[str, float] = {}
_sessions: Dict[str, requests.Session] = {}
_host_state_lock = Lock()
_MIN_INTERVAL_SECONDS = 1.2  # ~1 chamada a cada 1.2s por host regional

PLATFORM_TO_REGIONAL = {
    "BR1": "americas", "LA1": "americas", "LA2": "americas",
    "NA1": "americas", "OC1": "americas",
    "EUW1": "europe", "EUN1": "europe", "TR1": "europe", "RU": "europe",
    "JP1": "asia", "KR": "asia",
}


def get_regional_host(platform_region: str) -> str:
    regional = PLATFORM_TO_REGIONAL.get(platform_region, "americas")
    return f"https://{regional}.api.riotgames.com"


def get_json_with_backoff(url: str, max_retries: int = 3, initial_delay: float = 1.5, timeout: float = 10.0):
    """Faz requisições HTTP com backoff exponencial e rate limiting por host."""
    delay = initial_delay
    host = urlparse(url).netloc

    for _ in range(max_retries):
        # Throttle por host
        with _host_state_lock:
            now = time.time()
            last = _last_call_per_host.get(host, 0.0)
            wait = (last + _MIN_INTERVAL_SECONDS) - now
        if wait > 0:
            time.sleep(wait)

        try:
            with _host_state_lock:
                _last_call_per_host[host] = time.time()
                session = _sessions.get(host)
                if session is None:
                    session = requests.Session()
                    _sessions[host] = session
            response = session.get(url, timeout=timeout)
        except (requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.RequestException):
            time.sleep(delay + random.uniform(0, 0.25))
            delay = min(delay * 2, 10)
            continue

        if response.status_code == 429:
            retry_after = response.headers.get('Retry-After')
            sleep_time = float(retry_after) + 0.1 if retry_after else delay
            time.sleep(sleep_time)
        elif 500 <= response.status_code < 600:
            time.sleep(delay + random.uniform(0, 0.25))
        elif response.ok:
            try:
                return response.json()
            except json.JSONDecodeError:
                time.sleep(delay)
        else:
            # 4xx (exceto 429) — não retenta
            return None

        delay = min(delay * 2, 10)

    return None


def _format_eta(seconds_total: float) -> str:
    seconds_total = int(max(0, seconds_total))
    h = seconds_total // 3600
    m = (seconds_total % 3600) // 60
    s = seconds_total % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"


def main():
    parser = argparse.ArgumentParser(
        description="Coleta Match IDs a partir de PUUIDs usando Match V5 API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Pipeline de coleta:
  1. python pythoncode/matchIDscraper.py          → puuid.csv
    2. python pythoncode/collect_match_ids.py        → D:/Data/MatchIds.csv   (este script)
  3. python pythoncode/collect_10min_timeline.py   → MatchTimelineAllMinutes.csv
        """
    )
    parser.add_argument("--input", default="puuid.csv", help="CSV de PUUIDs (colunas: PUUID, Region)")
    parser.add_argument("--output", default="D:/Data/MatchIds.csv", help="CSV de saída (colunas: matchId, Region, blueWin)")
    parser.add_argument("--count", type=int, default=5, help="Número de partidas recentes por PUUID (default: 5)")
    parser.add_argument("--max-puuids", type=int, default=None, help="Limite de PUUIDs a processar (para testes)")

    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"[ERROR] Arquivo de entrada '{args.input}' não encontrado!", flush=True)
        print("        Execute primeiro: python pythoncode/matchIDscraper.py", flush=True)
        return

    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    # =========================================================================
    # CARREGA PUUIDs
    # =========================================================================
    print("=" * 80, flush=True)
    print("COLETA DE MATCH IDs - LEAGUE OF LEGENDS (Match V5 API)", flush=True)
    print("=" * 80, flush=True)

    df = pd.read_csv(args.input, dtype=str, keep_default_na=False)
    if "PUUID" not in df.columns or "Region" not in df.columns:
        print(f"[ERROR] CSV deve conter colunas 'PUUID' e 'Region'. Encontradas: {list(df.columns)}", flush=True)
        return

    df = df.drop_duplicates(subset=["PUUID", "Region"]).reset_index(drop=True)
    if args.max_puuids:
        df = df.head(args.max_puuids)

    print(f"[OK] {len(df)} PUUIDs únicos carregados de '{args.input}'", flush=True)

    # =========================================================================
    # DETECTA PARTIDAS JÁ PROCESSADAS + PUUIDs JÁ PROCESSADOS
    # =========================================================================
    output_path = args.output
    processed_match_ids: Set[str] = set()
    processed_puuids: Set[str] = set()
    output_exists = False

    # Arquivo auxiliar para rastrear PUUIDs já processados (permite retomar)
    puuids_done_path = os.path.splitext(output_path)[0] + "_puuids_done.txt"

    if os.path.exists(output_path):
        try:
            file_size = os.path.getsize(output_path)
            if file_size > 0:
                existing = pd.read_csv(output_path, dtype=str)
                if "matchId" in existing.columns:
                    processed_match_ids = set(existing["matchId"].astype(str).tolist())
                output_exists = True
                print(f"[INFO] {len(processed_match_ids)} partidas já coletadas em '{output_path}'", flush=True)
        except Exception as e:
            print(f"[WARN] Erro ao ler arquivo existente: {e}", flush=True)

    if os.path.exists(puuids_done_path):
        try:
            with open(puuids_done_path, "r") as f:
                processed_puuids = set(line.strip() for line in f if line.strip())
            print(f"[INFO] {len(processed_puuids)} PUUIDs já processados (serão pulados)", flush=True)
        except Exception:
            pass

    # =========================================================================
    # PREPARA FILA GLOBAL EMBARALHADA (distribuição uniforme entre regiões)
    # =========================================================================
    # Embaralha TODOS os PUUIDs globalmente para que, ao parar cedo,
    # a amostra seja bem distribuída entre todas as regiões/continentes.
    all_items = [
        {"PUUID": row["PUUID"], "Region": row["Region"]}
        for _, row in df.iterrows()
        if row["PUUID"] not in processed_puuids
    ]
    random.shuffle(all_items)

    print(f"[INFO] {len(all_items)} PUUIDs restantes para processar (após filtrar já processados)", flush=True)

    if not all_items:
        print("[OK] Todos os PUUIDs já foram processados!", flush=True)
        return

    # Distribui em filas por continente (para respeitar rate limit por host)
    queues: Dict[str, Queue] = {
        "americas": Queue(), "europe": Queue(), "asia": Queue(),
    }
    continent_counts = {"americas": 0, "europe": 0, "asia": 0}
    for item in all_items:
        continent = PLATFORM_TO_REGIONAL.get(item["Region"], "americas")
        queues[continent].put(item)
        continent_counts[continent] += 1

    for continent, count in continent_counts.items():
        if count > 0:
            print(f"  [{continent}] {count} PUUIDs na fila", flush=True)

    # Locks e estatísticas compartilhados
    write_lock = Lock()
    matches_lock = Lock()
    stats_lock = Lock()
    puuids_done_lock = Lock()

    stats = {
        "puuids_processed": 0,
        "puuids_total": len(all_items),
        "matches_written": 0,
        "matches_skipped_existing": 0,
        "matches_skipped_invalid": 0,
        "puuids_no_matches": 0,
        "start_time": time.time(),
    }

    count_per_puuid = args.count

    # =========================================================================
    # WORKER: processa PUUIDs de uma fila continental
    # =========================================================================
    def worker(continent: str, q: Queue):
        print(f"[THREAD-{continent}] Iniciado com {q.qsize()} PUUIDs", flush=True)

        while not q.empty():
            try:
                rec = q.get_nowait()
            except Exception:
                break

            puuid = rec["PUUID"]
            platform_region = rec["Region"]
            regional_host = get_regional_host(platform_region)

            # 1) Busca IDs das últimas N partidas ranqueadas
            url_matchlist = (
                f"{regional_host}/lol/match/v5/matches/by-puuid/{puuid}/ids"
                f"?queue=420&type=ranked&start=0&count={count_per_puuid}&api_key={api_key}"
            )
            match_ids = get_json_with_backoff(url_matchlist)
            if not isinstance(match_ids, list) or len(match_ids) == 0:
                with stats_lock:
                    stats["puuids_no_matches"] += 1
                    stats["puuids_processed"] += 1
                # Marca PUUID como processado mesmo sem resultados
                with puuids_done_lock:
                    with open(puuids_done_path, "a") as f:
                        f.write(puuid + "\n")
                continue

            for match_id in match_ids:
                # Verifica se já coletamos esta partida
                with matches_lock:
                    if match_id in processed_match_ids:
                        stats["matches_skipped_existing"] += 1
                        continue
                    processed_match_ids.add(match_id)

                # 2) Busca detalhes da partida para extrair blueWin
                url_match = f"{regional_host}/lol/match/v5/matches/{match_id}?api_key={api_key}"
                match_json = get_json_with_backoff(url_match)
                if not isinstance(match_json, dict):
                    continue

                info = match_json.get("info", {})
                metadata = match_json.get("metadata", {})

                # Valida que é ranked solo/duo (420) com 10 jogadores
                if info.get("queueId") != 420:
                    with stats_lock:
                        stats["matches_skipped_invalid"] += 1
                    continue

                participants = info.get("participants", [])
                teams = info.get("teams", [])
                if len(participants) != 10 or len(teams) != 2:
                    with stats_lock:
                        stats["matches_skipped_invalid"] += 1
                    continue

                # Extrai blueWin
                blue_win = None
                for t in teams:
                    if t.get("teamId") == 100:
                        blue_win = 1 if t.get("win") else 0
                        break

                if blue_win is None:
                    with stats_lock:
                        stats["matches_skipped_invalid"] += 1
                    continue

                # Extrai região do matchId (ex: "BR1_12345" → "BR1")
                try:
                    region_from_id = str(match_id).split("_")[0]
                except Exception:
                    region_from_id = platform_region

                # 3) Salva CADA partida imediatamente no CSV
                row_dict = {
                    "matchId": metadata.get("matchId", str(match_id)),
                    "Region": region_from_id,
                    "blueWin": blue_win,
                }
                with write_lock:
                    nonlocal output_exists
                    mode = "a" if output_exists else "w"
                    header = not output_exists
                    pd.DataFrame([row_dict]).to_csv(
                        output_path, mode=mode, index=False, header=header
                    )
                    output_exists = True
                with stats_lock:
                    stats["matches_written"] += 1

            # 4) Marca PUUID como processado (para retomada)
            with puuids_done_lock:
                with open(puuids_done_path, "a") as f:
                    f.write(puuid + "\n")

            # 5) Atualiza progresso
            with stats_lock:
                stats["puuids_processed"] += 1
                p = stats["puuids_processed"]
                total = stats["puuids_total"]
                if p % 100 == 0 or p == total:
                    elapsed = time.time() - stats["start_time"]
                    speed = p / max(elapsed / 60, 1e-6)
                    eta = (total - p) / max(speed, 1e-6) * 60
                    print(
                        f"[PROGRESSO] {p}/{total} PUUIDs ({p*100//total}%) | "
                        f"escritas={stats['matches_written']} | "
                        f"existentes={stats['matches_skipped_existing']} | "
                        f"inválidas={stats['matches_skipped_invalid']} | "
                        f"{speed:.1f} puuids/min | ETA {_format_eta(eta)}",
                        flush=True,
                    )

        print(f"[THREAD-{continent}] Concluído", flush=True)

    # =========================================================================
    # LANÇA THREADS (uma por continente com PUUIDs pendentes)
    # =========================================================================
    active_queues = {k: v for k, v in queues.items() if not v.empty()}
    threads: List[Thread] = []

    for continent, q in active_queues.items():
        t = Thread(target=worker, args=(continent, q), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # =========================================================================
    # RESUMO FINAL
    # =========================================================================
    total_time = time.time() - stats["start_time"]
    print("\n" + "=" * 80, flush=True)
    print("COLETA DE MATCH IDs CONCLUÍDA", flush=True)
    print("=" * 80, flush=True)
    print(f"  PUUIDs processados:    {stats['puuids_processed']}", flush=True)
    print(f"  PUUIDs sem partidas:   {stats['puuids_no_matches']}", flush=True)
    print(f"  Partidas gravadas:     {stats['matches_written']}", flush=True)
    print(f"  Partidas já existentes:{stats['matches_skipped_existing']}", flush=True)
    print(f"  Partidas inválidas:    {stats['matches_skipped_invalid']}", flush=True)
    print(f"  Tempo total:           {_format_eta(total_time)}", flush=True)
    print(f"  Arquivo de saída:      {output_path}", flush=True)

    # Deduplicação final por segurança
    if os.path.exists(output_path):
        try:
            df_out = pd.read_csv(output_path, dtype=str)
            before = len(df_out)
            df_out = df_out.drop_duplicates(subset=["matchId"])
            after = len(df_out)
            if before != after:
                df_out.to_csv(output_path, index=False)
                print(f"  Deduplicação: {before} → {after} ({before - after} duplicatas removidas)", flush=True)
            print(f"\n[OK] {after} partidas únicas em '{output_path}'", flush=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
