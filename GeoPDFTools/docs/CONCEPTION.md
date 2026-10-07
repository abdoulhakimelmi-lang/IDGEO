# GeoPDF Tools — Document de conception

Boîte à outils Python (`.pyt`) pour ArcGIS Pro **Standard** permettant d'associer
automatiquement des documents PDF aux entités d'une couche SIG, à partir d'un
identifiant présent dans le nom du fichier et/ou dans le texte du PDF.

- Dépendances : ArcPy (fourni avec ArcGIS Pro) et PyMuPDF (`import pymupdf`).
- Aucune extension (Spatial Analyst, Network Analyst…) ni licence Advanced.

---

## 1. Compatibilité de licence

| Opération | Fonction ArcPy | Niveau de licence |
|---|---|---|
| Lire les identifiants | `arcpy.da.SearchCursor` | Basic |
| Lister / vérifier les champs | `arcpy.ListFields`, `arcpy.Describe` | Basic |
| Créer le champ résultat | `arcpy.management.AddField` | Basic |
| Écrire les liens | `arcpy.da.UpdateCursor` (champ résultat uniquement) | Basic |
| Données versionnées / SDE | `arcpy.da.Editor` | Basic |
| Progression / messages | `SetProgressor`, `SetProgressorPosition`, `AddMessage`, `AddWarning`, `AddError` | — |
| V2 : pièces jointes | `EnableAttachments`, `AddAttachments` | Basic |

Tout fonctionne en Basic, donc en Standard. Le `UpdateCursor` n'ouvre que
`OID@` et le champ résultat : jamais `SHAPE@`, jamais `deleteRow()`.

## 2. Décisions validées

| Sujet | Décision |
|---|---|
| Nom du package | `geopdf_core/` (évite la collision du nom générique `src`) |
| Paramètres ajoutés | Créer le champ s'il n'existe pas (défaut : oui) ; mode d'écriture `Compléter` (défaut) / `Remplacer` ; sortie dérivée pour ModelBuilder |
| Sélection active | Si une sélection existe : seules les entités sélectionnées sont traitées. Sinon : toute la couche. Le comportement est annoncé dans les messages ArcGIS Pro |
| Contenu du champ résultat | Chemin **complet** du PDF ; plusieurs PDF séparés par ` \| ` |
| Casse | Recherche insensible à la casse |
| Formats | V1 testée sur File Geodatabase ; compatibilité SDE prévue (`arcpy.da.Editor`) ; shapefile accepté avec avertissement (champ texte limité à 254 caractères) |
| Sécurité | Aucune modification de géométrie, aucune suppression d'entité, de champ ou de PDF ; les PDF sont ouverts en lecture seule |

## 3. Arborescence

```
GeoPDFTools/
├── GeoPDFTools.pyt          # Toolbox ArcGIS : paramètres, validation, orchestration (étape 5)
├── geopdf_core/
│   ├── __init__.py
│   ├── dependencies.py      # Vérification de PyMuPDF                         (étape 1)
│   ├── models.py            # Statuts, modes, structures de données           (étape 1)
│   ├── pdf_reader.py        # Recherche des PDF, lecture du texte             (étape 1)
│   ├── matcher.py           # Normalisation, index et recherche des ID        (étape 2)
│   ├── report.py            # Rapport CSV et résumé                           (étape 3)
│   ├── arcgis_utils.py      # Seul module important arcpy                     (étape 4)
│   └── link_writers.py      # FieldLinkWriter (V1), AttachmentLinkWriter (V2) (étape 4)
├── tests/                   # Tests pytest exécutables sans ArcGIS
├── docs/CONCEPTION.md
├── requirements.txt
├── requirements-dev.txt
├── pytest.ini
└── README.md                                                                  (étape 5)
```

Principe : **seuls `arcgis_utils.py`, `link_writers.py` et le `.pyt` importent
arcpy**. Le reste est du Python pur, testable avec pytest hors d'ArcGIS Pro.

## 4. Paramètres de l'outil « Associer PDF aux entités SIG »

| # | Libellé | Type | Remarque |
|---|---|---|---|
| 0 | Couche SIG | `GPFeatureLayer` | |
| 1 | Champ identifiant | `Field` | Dépend de 0 ; Texte, Entier, GUID |
| 2 | Dossier des PDF | `DEFolder` | Parcours récursif |
| 3 | Nom du champ résultat | `GPString` | Défaut `LIEN_PDF` |
| 4 | Mode de recherche | `GPString` (liste) | `Nom du fichier` / `Contenu du PDF` / `Nom + contenu` (défaut) |
| 5 | Rapport CSV | `DEFile` sortie, facultatif | |
| 6 | Créer le champ s'il n'existe pas | `GPBoolean` | Défaut : coché |
| 7 | Mode d'écriture | `GPString` (liste) | `Compléter` (défaut) / `Remplacer` |
| 8 | Couche mise à jour | Sortie dérivée | Pour ModelBuilder |
| 9 | ☐ Recherche tolérante | `GPBoolean` | Défaut : **décoché** (recherche stricte). À activer si l'extraction produit `N57 _001` ou `N57-` / `001` |

`allow_risky_in_content` (recherche des identifiants courts ou numériques dans le
texte) **n'est pas exposé** dans l'interface : ces identifiants restent protégés.

Un outil de géotraitement ne peut pas ouvrir de boîte de dialogue en cours
d'exécution : la « proposition » de création du champ se fait par un
avertissement dans `updateMessages` + la case à cocher n°6.

## 5. Déroulement du traitement

1. Vérifier PyMuPDF (`dependencies.check_pymupdf`).
2. Lire une seule fois les identifiants : `{id_normalisé: [OID, …]}` (doublons signalés, nulls ignorés).
3. Construire un moteur de recherche unique (regex « trie » avec bornes alphanumériques).
4. Lister récursivement les `*.pdf` (extension insensible à la casse), initialiser la barre de progression.
5. Pour chaque PDF : ouverture PyMuPDF, texte page par page, gestion des PDF chiffrés / corrompus / sans texte.
6. Recherche selon le mode (nom, contenu, ou union des deux).
7. Statut : 0 ID → `AUCUNE_CORRESPONDANCE`, 1 → `ASSOCIE`, ≥ 2 → `PLUSIEURS_CORRESPONDANCES` (associé à toutes les entités).
8. Agrégation `{OID: [chemins]}` en mémoire.
9. Écriture **après** la lecture de tous les PDF (pas d'écriture partielle si la lecture échoue) : un seul `UpdateCursor`, uniquement les lignes concernées, fusion sans doublon en mode `Compléter`.
10. Rapport CSV (UTF-8 avec BOM, séparateur `;`) et résumé dans ArcGIS Pro.

## 6. Rapport CSV (`report.py`)

UTF-8 avec BOM, séparateur `;`, une ligne par PDF analysé.

| Colonne | Contenu |
|---|---|
| `nom_pdf` | Nom du fichier |
| `chemin_pdf` | Chemin complet |
| `statut` | `ASSOCIE`, `PLUSIEURS_CORRESPONDANCES`, `AUCUNE_CORRESPONDANCE`, `PDF_SANS_TEXTE`, `PDF_ILLISIBLE` |
| `identifiant_trouve` | Identifiants détectés, séparés par ` \| ` |
| `source_detection` | `NOM`, `CONTENU` ou `NOM+CONTENU` (global au PDF) |
| `nombre_correspondances` | Nombre d'identifiants distincts |
| `nombre_entites` | Nombre d'entités (différent si identifiant dupliqué dans la couche) |
| `nombre_pages` | Nombre de pages (0 si le PDF n'a pas pu être ouvert) |
| `contenu_pdf` | `EXPLOITABLE`, `ILLISIBLE`, `SANS_TEXTE`, `NON_ANALYSE` (mode « Nom du fichier ») |
| `avertissement` | Avertissements (doublons, plusieurs identifiants, contenu non exploitable, MuPDF) |
| `erreur` | Erreur de lecture du PDF |
| `detail_sources` | Source par identifiant, ex. `N57_001 [NOM+CONTENU] \| A31_0042 [CONTENU]` |
| `oid_entites` | ObjectID des entités concernées |

**Association par le nom avec contenu non exploitable** (décision validée) : un PDF
illisible ou scanné dont le nom contient un identifiant est `ASSOCIE`. Pas de statut
supplémentaire : `contenu_pdf` vaut `ILLISIBLE` / `SANS_TEXTE`, la colonne
`avertissement` contient « Contenu PDF non exploitable (…) : association par le nom du
fichier uniquement », et le résumé compte ces cas séparément.

Protections : les cellules commençant par `= + - @` sont préfixées d'une apostrophe
(pas d'exécution de formule dans Excel) ; les retours à la ligne sont remplacés par des
espaces ; écriture dans un fichier temporaire puis renommage (un rapport ouvert dans
Excel n'est jamais à moitié écrit).

Résumé (`summarize(...).messages()`), prêt pour `AddMessage` / `AddWarning` :

```
PDF analysés : 250
Associés : 221
Plusieurs correspondances : 7
Sans correspondance : 19
PDF sans texte : 2
PDF illisibles : 1
Associés par le nom avec contenu PDF non exploitable : 3   (si > 0)
Entités concernées : 228
```

## 7. Risques techniques

| # | Risque | Mesure |
|---|---|---|
| R1 | Identifiants courts / numériques → faux positifs | Avertissement si < 4 caractères ou purement numérique |
| R2 | Texte PDF altéré (`N57 _002`, césure, ligatures) | Normalisation NFKC + majuscules ; option de tolérance aux séparateurs |
| R3 | PDF scannés sans texte | Statut `PDF_SANS_TEXTE` ; détection possible par le nom de fichier ; OCR hors V1 |
| R4 | Longueur du champ texte (shapefile : 254) | Champ créé large en GDB ; jamais de troncature silencieuse ; avertissement + rapport |
| R5 | Sélection / requête de définition | Comportement validé (§2) et annoncé dans les messages |
| R6 | Données versionnées / SDE | `arcpy.da.Editor` si nécessaire |
| R7 | Verrous | Message explicite ; lecture complète avant toute écriture |
| R8 | Ré-exécution | Mode `Compléter` = fusion sans doublon ; entités sans correspondance non modifiées |
| R9 | Cache des modules dans ArcGIS Pro | Package au nom unique, `sys.path` relatif au `.pyt`, `importlib.reload` en développement |
| R10 | Installation de PyMuPDF | Cloner `arcgispro-py3`, puis `pip install pymupdf` ; repli sur `import fitz` pour PyMuPDF < 1.24.3 |
| R11 | Licence PyMuPDF (AGPL v3 / commerciale) | À vérifier avant toute diffusion externe |
| R12 | Volumétrie (gros PDF, milliers de fichiers) | Lecture page par page, progression par fichier, pas de multiprocessing en V1 |
| R13 | Chemins réseau / accents | `pathlib`, chemins absolus, UTF-8 |

## 8. Plan de développement

1. **Étape 1** : `models.py`, `dependencies.py`, `pdf_reader.py` + tests.
2. **Étape 2** : `matcher.py` + tests.
3. **Étape 3** : `report.py` + tests.
4. **Étape 4** : `arcgis_utils.py` + `link_writers.py`.
5. **Étape 5** : `GeoPDFTools.pyt` + `README.md`.

## 9. Évolutions prévues (V2+)

- `AttachmentLinkWriter` : stockage en pièces jointes ArcGIS (`EnableAttachments` / `AddAttachments`).
- Limite du nombre de pages lues par PDF (paramètre déjà présent dans `pdf_reader.read_pdf`).
- OCR (Tesseract) pour les PDF scannés.

## 10. Règles de correspondance (étape 2, `matcher.py`)

| Règle | Exemple accepté | Exemple refusé |
|---|---|---|
| Insensible à la casse et aux variantes Unicode (NFKC, espaces insécables, tirets typographiques, caractères invisibles) | `rn57–025` → `RN57-025` | — |
| Pas collé à une lettre ou un chiffre | `inspection_N57_001_v2` | `N57_0010` pour `N57_001` |
| Identifiant commençant/finissant par un chiffre : ne prolonge pas une suite numérique (`_ - / . , + :` suivi d'un chiffre) | `N57_001 15/09/2026` | `PR12+3500`, `RN57-025,5`, `rapport_2026_001` pour `2026` |
| L'identifiant le plus long l'emporte | `N57_001_A` (si présent dans la couche) | — |
| Identifiants courts (< 4 caractères) ou numériques (< 6 chiffres) : nom de fichier uniquement | `12.pdf` pour `12` | « page 12 » dans le texte |
| Mode tolérant (option, désactivé par défaut) : séparateurs interchangeables | `N57 _001`, `N57-⏎001` | `N57001` (séparateur absent) |

Statut d'un PDF : ≥ 1 identifiant → `ASSOCIE` / `PLUSIEURS_CORRESPONDANCES`, même si le
contenu est illisible mais que le nom a suffi (l'erreur reste dans le rapport). Sinon :
`PDF_ILLISIBLE` / `PDF_SANS_TEXTE` si le contenu devait être lu, sinon `AUCUNE_CORRESPONDANCE`.

## 11. Limites acceptées pour la V1

- Plages d'identifiants (`N57_001 à N57_003`, `SEC-2026-001/002`) : non interprétées.
- Suffixes après un séparateur (`SEC-2026-001-BIS`, `N57_001_B`) : reconnus comme l'identifiant de base s'il n'existe pas de variante dans la couche.
- Identifiant collé à un mot (`SectionN57_001`) : refusé.
- Pas d'OCR pour les PDF scannés.
- Confusions `O`/`0` (OCR de mauvaise qualité) : non corrigées.
- Zéros de tête : `N57_1` ≠ `N57_001`.
- Mots sans chiffre (`NORD`) : simple avertissement.
- Identifiants courts / numériques : cherchés uniquement dans le nom des fichiers.
- Mode tolérant : `N57_002` et `N57-002` sont fusionnés (traités comme doublon).

## 12. Mode « Nom du fichier » (décision validée)

En mode `Nom du fichier`, le PDF **n'est pas ouvert** : aucune lecture PyMuPDF
(performances, aucune lecture inutile). Conséquences dans le rapport :
`contenu_pdf = NON_ANALYSE`, `nombre_pages` **vide** (inconnu, et non « 0 »),
`erreur` vide (un PDF corrompu n'est pas détecté dans ce mode).

## 13. Lecture de la couche et écriture des liens (étape 4)

### Périmètre traité
- Sélection active → seules les entités sélectionnées (message `WARNING` explicite).
- Aucune sélection → toute la couche.
- Requête de définition → respectée, jamais contournée (message `WARNING`).
- Les curseurs sont ouverts **sur la couche** : ArcGIS applique lui-même sélection et filtre.

### Champ résultat
| Format | Création | Capacité |
|---|---|---|
| Géodatabase fichier | Texte 4000 | 4000 caractères |
| Géodatabase d'entreprise | Texte 2000 (prudent : Oracle NVARCHAR2) | 2000 caractères — **non testé** |
| Shapefile | Texte 254 + avertissement ; nom ≤ 10 caractères | 254 **octets** (mesure prudente) — **non testé** |
| Champ existant | Utilisé tel quel s'il est de type texte et modifiable | Sa longueur réelle |

Refus : champ existant non texte (jamais converti), champ non modifiable, champ
identifiant / ObjectID / géométrie, création non autorisée, nom invalide.

### Écriture (`FieldLinkWriter`)
1. **Lecture** : `SearchCursor(["OID@", champ])`.
2. **Plan** (Python pur) : valeur finale par entité, `ECRIRE` / `INCHANGE` / `TROP_LONG`.
   Une valeur trop longue n'est **jamais tronquée** : l'entité n'est pas modifiée et
   chaque PDF concerné reçoit l'avertissement « Lien NON écrit… » dans le rapport.
3. **Écriture** : `UpdateCursor(["OID@", champ])`, `updateRow` uniquement pour les
   entités `ECRIRE`. Une entité dont la valeur a changé depuis la lecture n'est pas
   écrasée. Si rien n'est à écrire, aucun curseur d'écriture n'est ouvert.

Toutes les vérifications bloquantes (champ, type, nom, capacité) ont lieu **avant**
le curseur d'écriture : une erreur à ce stade ne modifie aucune entité.

### Transactions
- Géodatabase d'entreprise ou données versionnées : écriture dans `arcpy.da.Editor`
  (annulation automatique en cas d'erreur) — **non testé**.
- Géodatabase fichier / shapefile (V1) : pas de session de mise à jour. Une erreur
  *pendant* l'écriture (très improbable après les contrôles) laisse les entités déjà
  écrites ; le message d'erreur liste leurs OID.

### À tester réellement dans ArcGIS Pro (DIR Est)
Script fourni : `tests_arcgis/verifier_etape4.py` (géodatabase fichier + shapefile,
données temporaires). Restent à tester sur données réelles :
1. Géodatabase fichier : couche dans une carte, sélection, requête de définition.
2. Couche en cours de mise à jour dans ArcGIS Pro (session d'édition ouverte, verrous).
3. Géodatabase d'entreprise non versionnée (SQL Server / Oracle / PostgreSQL selon le site).
4. Géodatabase d'entreprise versionnée (branche ou traditionnelle) : `arcpy.da.Editor`, paramètre `multiuser_mode`.
5. Suivi des mises à jour (*editor tracking*) : ArcGIS met à jour `last_edited_user` / `last_edited_date` des entités écrites (comportement d'ArcGIS, non piloté par l'outil).
6. Règles attributaires / déclencheurs pouvant modifier d'autres champs.
7. Classes d'entités avec relations, topologie ou réseau.
8. Longueur réelle des champs créés selon le SGBD ; caractères accentués dans un shapefile.
9. Services d'entités (ArcGIS Online / Enterprise).
