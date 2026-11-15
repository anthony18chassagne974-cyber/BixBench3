# bixdash — dashboard des runs BixBench3

Deux morceaux indépendants :

```
VM de run (×N)                         bucket GCS                       où tu veux
┌──────────────────────────┐   gcloud storage cp   ┌───────────────────────────────┐   lecture   ┌──────────────┐
│ /opt/bixdash/collect.py   │ ───────────────────► │ gs://…-results/_dashboard/     │ ◄────────── │ server.py    │
│ timer systemd, 5 min      │                      │   <run_id>/meta.json           │             │ + index.html │
│ LECTURE SEULE du run      │                      │   <run_id>/latest.json         │             │ port 8050    │
└──────────────────────────┘                      │   <run_id>/history.jsonl       │             └──────────────┘
                                                   └───────────────────────────────┘
```

## Ce que le collecteur lit (sans rien modifier)

| Donnée | Source sur la VM |
|---|---|
| modèle, effort, tâche, grader, préfixe de résultats, `RUN_DESCRIPTION` | `/var/lib/bixbench3/run.sh` |
| phase / statut | `/var/lib/bixbench3/status.json` |
| début du run | `systemctl show bixbench3-run` |
| tokens, coût, nb d'appels, dernière commande, dernier message | buffer live d'Inspect `/root/.local/share/inspect_ai/samplebuffer/*/*.db` (SQLite ouvert en `mode=ro`) |
| artefacts attendus / écrits | `dataset/case.json` + `outputs/` |
| avancement spécifique (ex. 37/85 échantillons) | glob configurable dans `workspace/work/` |
| agents actifs dans le temps + table par agent (appels, tokens, coût) | spans Inspect de type `agent` dans le buffer (`agent()`, `as_tool`, `handoff`, `run`) ; sinon `role` ; sinon « main » |
| généré vs référence par artefact | détails des checks du grader (lignes trouvées / manquantes / en trop, précision, rappel, CCC sur les lignes communes) — comptes seulement, aucune clé ni valeur de la référence |
| CPU / RAM / disque | `/proc`, `os.getloadavg`, `df` |
| **score partiel** | copie de `outputs/` → grader officiel (`grade_existing_artifacts`), toutes les 15 min |

Il écrit uniquement dans `/var/lib/bixdash/` et `gs://<bucket>/_dashboard/<run_id>/`.
Il ne touche ni `/opt/bixbench3`, ni `/var/lib/bixbench3`, ni `bixbench3-run.service`, ni le sandbox.

**Intégrité de la notation partielle** : la vérité terrain est téléchargée par le
grader officiel dans un dossier temporaire `0700` sous `/var/lib/bixdash/gt/` sur
l'**hôte**, supprimé à la fin de chaque notation. Ce dossier n'est jamais monté dans
le sandbox de l'agent — même mécanisme que la notation finale de `run.sh`.

**Coût** : calculé depuis les tokens avec les prix publics (USD / 1M tokens),
modifiables dans `/var/lib/bixdash/prices.json` :
`{"openai/gpt-5.4-mini": {"input": 0.75, "cached": 0.075, "output": 4.50}}`.

**Temps restant** : estimation grossière (rythme de production des artefacts, ou
rythme du compteur pendant une phase comptée), toujours bornée par la limite de 24 h.
Le dashboard affiche aussi la fin maximale (début + 24 h) et la signale si elle tombe
après la deadline du hackathon.

## 1. Installer le collecteur sur une VM (run actif inclus)

Copier le dossier `vm/` sur la VM (Cloud Shell : `gcloud compute scp --recurse vm <VM>:~/bixdash-vm --zone=… --project=…`,
ou bouton « Upload file » du SSH navigateur), puis :

```bash
cd ~/bixdash-vm
sudo bash install_collector.sh \
  --description "Baseline : harness Inspect d'origine, GPT-5.4 mini xhigh, VM 8 vCPU sans Cell Ranger" \
  --condition baseline \
  --progress-label "Échantillons Tempo-seq comptés" --progress-glob "counts/*" --progress-total 85
```

Vérifier : `sudo cat /var/lib/bixdash/latest.json | head -40` et
`gcloud storage ls gs://bixbench3-task11-jex20-results/_dashboard/`.

Si `tokens` est vide : chercher le buffer (`sudo find / -path '*samplebuffer*' -name '*.db' 2>/dev/null`)
et mettre `Environment=BIXDASH_BUFFER_GLOB=<chemin>/*.db` dans `/etc/systemd/system/bixdash-collect.service`.

Désinstaller : `sudo systemctl disable --now bixdash-collect.timer`.

## 2. Pour chaque nouveau run : la phrase « ce qu'on essaie »

Une ligne par run, obligatoire, écrite **avant** le lancement :

* soit en tête de `run.sh` (commentaire, sans effet sur l'exécution) :
  ```bash
  # RUN_DESCRIPTION: Swarm v1 — proposer/experimenter/critic, le critic valide chaque nœud du DAG avant la suite
  ```
* soit via `install_collector.sh --description "…" --condition swarm-v1 --baseline-run-id <run_id de la baseline>`
  (prioritaire sur run.sh).

`--condition` sert de filtre dans le dashboard ; `--baseline-run-id` choisit la
courbe de comparaison (sinon : la baseline de la même tâche).

Checklist de lancement d'une variante :
1. nouveau préfixe de résultats (`…/<paper_id>/<model>/<timestamp>/`), jamais celui d'un autre run ;
2. diff du harnais vs commit d0e0bbb archivé avec le run (`harness.diff`) ;
3. `RUN_DESCRIPTION` + `--condition` renseignés ;
4. collecteur installé dès le démarrage, pour avoir la courbe complète.

## Importer un run terminé sans collecteur

```bash
python3 tools/import_finished_run.py gs://…-results/<paper_id>/<model>/<timestamp> \
  --description "…" --condition baseline
```
Reconstruit coût, tokens, appels et agents actifs dans le temps depuis la trace `.eval`,
plus le score final et les détails par artefact. Pas de courbe de score partiel.

**Swarms** : pour que chaque agent soit compté, lancez vos sous-agents via les primitives
Inspect (`agent()`/`run()`, `as_tool`, `handoff`) qui ouvrent un span de type `agent`
nommé d'après l'agent. Des agents lancés hors Inspect (autre SDK, sous-processus) ne sont
pas visibles dans la trace.

## 3. Lancer le site

```bash
pip install google-cloud-storage zstandard     # zstandard : lecture des .eval d'Inspect (Python < 3.14)
gcloud auth application-default login          # une fois
python3 dashboard/server.py --source gs://bixbench3-task11-jex20-results/_dashboard --port 8050
# → http://localhost:8050
```

Hors ligne / démo : `python3 tools/make_demo.py demo_data && python3 dashboard/server.py --source demo_data`.

Mot de passe partagé : `BIXDASH_TOKEN=un-secret python3 dashboard/server.py …`, puis
envoyer `http://<hôte>:8050/?token=un-secret` (cookie posé, 7 jours).

### Partager avec l'équipe

* **Depuis ton laptop** : `cloudflared tunnel --url http://localhost:8050` donne une URL publique
  temporaire ; garde `BIXDASH_TOKEN` activé. S'arrête quand le laptop dort.
* **Toujours allumé** : une petite VM `e2-small` dans le projet GCP est le plus simple
  (accès natif au bucket via son compte de service, pas de clé à copier).
  Un nœud Stanford marche aussi si les processus longs y sont autorisés : il faut alors un
  compte de service limité à `roles/storage.objectViewer` sur le bucket
  (`GOOGLE_APPLICATION_CREDENTIALS=key.json`) et un tunnel (cloudflared ou `ssh -L`) pour
  les coéquipiers sans accès SUNet.
