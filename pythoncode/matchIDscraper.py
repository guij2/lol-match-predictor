#Required imports
import requests
import json
import time
import os
import random
import pandas as pd
from collections import defaultdict


#Information for Riot API
api_key = os.environ.get("RIOT_API_KEY", "RGAPI-YOUR-KEY-HERE")



# Simple helper for retries/backoff and rate-limit handling
def get_json_with_backoff(url, max_retries=3, initial_delay_seconds=1.5, timeout_seconds=10):
    delay_seconds = initial_delay_seconds
    for attempt_index in range(max_retries):
        try:
            response = requests.get(url, timeout=timeout_seconds)
        except (requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.RequestException):
            time.sleep(delay_seconds + random.uniform(0, 0.25))
            delay_seconds = min(delay_seconds * 2, 10)
            continue

        if response.status_code == 429:
            retry_after_header = response.headers.get('Retry-After')
            if retry_after_header:
                try:
                    time.sleep(float(retry_after_header) + 0.1)
                except ValueError:
                    time.sleep(delay_seconds)
            else:
                time.sleep(delay_seconds)
        elif 500 <= response.status_code < 600:
            # Handle transient server errors with backoff
            time.sleep(delay_seconds + random.uniform(0, 0.25))
        elif response.ok:
            try:
                return response.json()
            except json.JSONDecodeError:
                time.sleep(delay_seconds)
        else:
            time.sleep(delay_seconds)

        delay_seconds = min(delay_seconds * 2, 10)

    return None


# Contadores de progresso por região
region_inserts = defaultdict(int)
region_completed_calls = defaultdict(int)
region_start_ts = {}
TOTAL_PAGES_PER_REGION = 6*4*19  # ranks x tiers x pages
TOTAL_CALLS_PER_REGION = TOTAL_PAGES_PER_REGION + 1  # +1 pelo Master+

def _format_eta(seconds_total):
    seconds_total = int(max(0, seconds_total))
    hours = seconds_total // 3600
    minutes = (seconds_total % 3600) // 60
    seconds = seconds_total % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


#Code for pulling list of summoner IDs

#Function to pull a single page of summoner IDs for given rank and tier
def summ_ID_puller(division,tier,page,region="BR1"):

    
    url_pull = "https://{}.api.riotgames.com/lol/league/v4/entries/RANKED_SOLO_5x5/{}/{}?page={}&api_key={}".format(region,division,tier,page,api_key)
    profile_list = get_json_with_backoff(url_pull)
    if not isinstance(profile_list, list):
        return
    num_profiles = len(profile_list)
    print(f"[{region}] {division} {tier} pág {page}: {num_profiles} perfis retornados", flush=True)
    puuid_entries = []
    rank_entries = []
    
    for profile_index in range(0, num_profiles):
        entry = profile_list[profile_index]
        puuid_value = entry.get('puuid')
        if not puuid_value:
            continue
        puuid_entries.append({
            'PUUID': puuid_value,
            'Region': region
        })
        # Persist rank from entries to speed up later stages
        tier_value = entry.get('tier')
        division_value = entry.get('rank')
        if tier_value:
            rank_entries.append({
                'PUUID': puuid_value,
                'Region': region,
                'Tier': tier_value,
                'Division': division_value if division_value else ''
            })
        
    if len(puuid_entries) > 0:
        df = pd.DataFrame(puuid_entries, columns=["PUUID", "Region"])
        df.to_csv('puuid.csv', mode='a', index=False, header=not os.path.exists('puuid.csv'))
        print(f"[{region}] {division} {tier} pág {page}: gravados {len(puuid_entries)} PUUIDs", flush=True)
        region_inserts[region] += len(puuid_entries)
    else:
        print(f"[{region}] {division} {tier} pág {page}: nenhum PUUID válido encontrado", flush=True)

    if len(rank_entries) > 0:
        df_rank = pd.DataFrame(rank_entries, columns=["PUUID", "Region", "Tier", "Division"])
        df_rank.to_csv('puuid_rank_from_entries.csv', mode='a', index=False, header=not os.path.exists('puuid_rank_from_entries.csv'))


# Function to pull PUUIDs for Master+, which use distinct league endpoints
def summ_ID_puller_master_plus(region="BR1"):

    high_tiers = [
        ("challenger", "challengerleagues"),
        ("grandmaster", "grandmasterleagues"),
        ("master", "masterleagues"),
    ]

    puuid_entries = []
    rank_entries = []

    total_written = 0
    for tier_name, league_endpoint in high_tiers:
        time.sleep(1.5)
        url_pull = "https://{}.api.riotgames.com/lol/league/v4/{}/by-queue/RANKED_SOLO_5x5?api_key={}".format(region, league_endpoint, api_key)
        league_json = get_json_with_backoff(url_pull)

        try:
            entries = league_json["entries"]
            print(f"[{region}] {tier_name}: {len(entries)} entradas retornadas", flush=True)
            for entry in entries:
                puuid_value = entry.get('puuid')
                if not puuid_value:
                    continue
                puuid_entries.append({
                    'PUUID': puuid_value,
                    'Region': region
                })
                tier_value = league_json.get('tier', tier_name.upper())
                division_value = entry.get('rank', 'I')
                rank_entries.append({
                    'PUUID': puuid_value,
                    'Region': region,
                    'Tier': tier_value,
                    'Division': division_value
                })
        except (KeyError, TypeError):
            # In case of unexpected response or rate limiting
            pass

    if len(puuid_entries) > 0:
        df = pd.DataFrame(puuid_entries, columns=["PUUID", "Region"])
        df.to_csv('puuid.csv', mode='a', index=False, header=not os.path.exists('puuid.csv'))
        total_written = len(puuid_entries)
        region_inserts[region] += total_written
    print(f"[{region}] Master+: gravados {total_written} PUUIDs", flush=True)

    if len(rank_entries) > 0:
        df_rank = pd.DataFrame(rank_entries, columns=["PUUID", "Region", "Tier", "Division"])
        df_rank.to_csv('puuid_rank_from_entries.csv', mode='a', index=False, header=not os.path.exists('puuid_rank_from_entries.csv'))

for rank in ["EMERALD", "DIAMOND", "PLATINUM", "GOLD", "SILVER", "BRONZE"]:
    for tier in ["I","II","III","IV"]:
        for page in range(1,20):
            time.sleep(1.5)
            for region in ["BR1","NA1","EUW1","EUN1","JP1","KR","LA1","LA2","OC1","TR1","RU"]:
                # Inicializa o relógio da região quando começar a primeira página
                if region not in region_start_ts:
                    region_start_ts[region] = time.time()
                print(f"Iniciando: [{region}] {rank} {tier} pág {page}", flush=True)
                summ_ID_puller(rank,tier,page,region=region)
                # Atualiza contagem de chamadas para ETA
                region_completed_calls[region] += 1
                elapsed = time.time() - region_start_ts[region]
                remaining_calls = max(0, TOTAL_CALLS_PER_REGION - region_completed_calls[region])
                avg_per_call = elapsed / max(1, region_completed_calls[region])
                eta_seconds = remaining_calls * avg_per_call
                print(f"[{region}] Progresso: {region_completed_calls[region]}/{TOTAL_CALLS_PER_REGION} chamadas, ETA ~ {_format_eta(eta_seconds)}", flush=True)

# Also include Master+ (Challenger/Grandmaster/Master) players per region
for region in ["BR1","NA1","EUW1","EUN1","JP1","KR","LA1","LA2","OC1","TR1","RU"]:
    print(f"Iniciando Master+ para região {region}", flush=True)
    summ_ID_puller_master_plus(region=region)
    region_completed_calls[region] += 1
    if region in region_start_ts:
        elapsed = time.time() - region_start_ts[region]
        remaining_calls = max(0, TOTAL_CALLS_PER_REGION - region_completed_calls[region])
        avg_per_call = elapsed / max(1, region_completed_calls[region])
        eta_seconds = remaining_calls * avg_per_call
        print(f"[{region}] Progresso: {region_completed_calls[region]}/{TOTAL_CALLS_PER_REGION} chamadas, ETA ~ {_format_eta(eta_seconds)}", flush=True)

# Deduplicate aggregated PUUIDs across all regions before proceeding
try:
    all_players_df = pd.read_csv('puuid.csv', dtype=str, keep_default_na=False)
    if 'PUUID' in all_players_df.columns and 'Region' in all_players_df.columns:
        antes = len(all_players_df)
        all_players_df = all_players_df.drop_duplicates(subset=['PUUID','Region'])
        depois = len(all_players_df)
        removidos = antes - depois
        all_players_df.to_csv('puuid.csv', index=False)
        print(f"Deduplicação concluída: {antes} → {depois} (removidos {removidos})", flush=True)
        # Resumos por região
        try:
            counts_final_region = (
                all_players_df.groupby('Region')['PUUID'].nunique().sort_index()
            )
            print("Inseridos (antes da deduplicação) por região:", flush=True)
            for reg in sorted(region_inserts.keys()):
                print(f"  {reg}: {region_inserts[reg]}", flush=True)

            print("Únicos (após deduplicação) por região:", flush=True)
            for reg, cnt in counts_final_region.items():
                print(f"  {reg}: {cnt}", flush=True)

            print("Comparação inseridos vs únicos por região:", flush=True)
            regions_union = sorted(set(list(region_inserts.keys()) + list(counts_final_region.index)))
            for reg in regions_union:
                inserted = region_inserts[reg]
                uniques = int(counts_final_region.get(reg, 0))
                print(f"  {reg}: {inserted} → {uniques}", flush=True)
        except Exception:
            pass
except Exception:
    pass

        
   
""" #Code for pulling list of account IDs from summoner IDs
summoner_IDs = pd.read_csv("summID.csv")
accountID_list = []

#Function to get the encrypted account ID from summoner ID
def acct_ID_puller(summID):
    url_acct_pull = "https://{}.api.riotgames.com/lol/summoner/v4/summoners/{}?api_key={}".format(region,summID,api_key)
    account_info = requests.get(url_acct_pull).json()
    accountID_list.append(account_info["accountId"])
    

summID_list = summoner_IDs["Summoner ID"]
for summID_idx in range(0,12000):
    time.sleep(1.5)
    if summID_list[summID_idx] == "Summoner ID":
        pass
    
    else:
        try:
            acct_ID_puller(summID_list[summID_idx])
        except KeyError:
            print("keyerror")
            
            
        
df = pd.DataFrame(accountID_list, columns = ["AccountId"])
df.to_csv('accountId.csv',mode = 'a', index=False)
print("Done pulling accounts!")


#Step 3: Pulling 5 most recent matches for each player
account_IDs = pd.read_csv("accountId.csv")
account_IDs_list = account_IDs["AccountId"]

#This is the list of MatchIDs we are creating
matchID_list = []

#Logging any errors that occur
pull_errors = []
    
#Function to pull the 5 most recent matches for a given account ID
def match_ID_puller(acctid):    
    url_match_pull = "https://{}.api.riotgames.com/lol/match/v4/matchlists/by-account/{}?queue=420&api_key={}".format(region,acctid,api_key)
    match_history = requests.get(url_match_pull).json()
    for i in range(0,5):        
        try:
            match_id = match_history['matches'][i]['gameId']
            matchID_list.append(match_id)
            
        except KeyError:
            print(match_history)
            print("KeyError occured with account:",acctid) 
            pull_errors.append(match_history)
  


for acct_id in accountID_list:
    time.sleep(1.5)
    if acct_id == "AccountId":
        pass
    else:
        match_ID_puller(acct_id)


df = pd.DataFrame(matchID_list, columns = ["MatchId"])
df.to_csv('MatchId.csv',mode = 'a', index=False)
print("Done pulling matchIDs!")
 """
    
    
    
