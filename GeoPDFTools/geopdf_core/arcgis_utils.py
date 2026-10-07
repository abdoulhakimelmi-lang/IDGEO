"""Accès à la couche SIG avec ArcPy (licence Basic suffisante).

Le module est organisé en deux parties :

- **logique pure** (aucun appel arcpy, testée avec pytest) : analyse des
  chemins, de la sélection, des champs, décision de création du champ ;
- **fonctions ArcPy** : ``arcpy`` n'est importé qu'à l'appel. Ces fonctions
  ne peuvent être vérifiées que dans ArcGIS Pro
  (voir ``tests_arcgis/verifier_etape4.py``).

Fonctions ArcPy utilisées (toutes disponibles en licence Basic) :
``arcpy.Describe``, ``arcpy.ListFields``, ``arcpy.ValidateFieldName``,
``arcpy.da.SearchCursor``, ``arcpy.da.UpdateCursor``, ``arcpy.da.Editor``,
``arcpy.management.AddField``, ``arcpy.management.GetCount``.

Règles de sécurité : jamais de ``SHAPE@``, jamais de suppression d'entité ou
de champ, jamais d'écriture ailleurs que dans le champ résultat.
"""

from __future__ import annotations

import ntpath
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from types import ModuleType
from typing import Any, Dict, Hashable, Iterable, Iterator, List, Optional, Sequence, Tuple

from .link_writers import FieldCapacity, FieldLinkWriter, LengthUnit
from .matcher import normalize_identifier
from .models import WriteMode

Message = Tuple[str, str]
"""``(niveau, texte)`` avec niveau ``INFO``, ``WARNING`` ou ``ERROR``."""


class ArcPyUnavailableError(ImportError):
    """ArcPy est absent : le code est exécuté hors d'ArcGIS Pro."""


def import_arcpy() -> ModuleType:
    try:
        import arcpy
    except ImportError as exc:
        raise ArcPyUnavailableError(
            "ArcPy est introuvable : cette fonction doit être exécutée avec le Python d'ArcGIS Pro."
        ) from exc
    return arcpy


# =============================================================================
# Partie 1 — logique pure (testable sans ArcGIS Pro)
# =============================================================================

# --- Format des données -----------------------------------------------------


class DataFormat(str, Enum):
    FILE_GDB = "Géodatabase fichier"
    ENTERPRISE_GDB = "Géodatabase d'entreprise"
    SHAPEFILE = "Shapefile"
    FEATURE_SERVICE = "Service d'entités"
    AUTRE = "Autre"


NEW_FIELD_LENGTH = {
    DataFormat.FILE_GDB: 4000,
    DataFormat.ENTERPRISE_GDB: 2000,  # prudent : limite des NVARCHAR2 Oracle (au-delà : NCLOB)
    DataFormat.SHAPEFILE: 254,  # limite du format DBF
    DataFormat.FEATURE_SERVICE: 2000,
    DataFormat.AUTRE: 2000,
}
"""Longueur du champ résultat lorsqu'il est créé par l'outil."""

FIELD_NAME_MAX_LENGTH = {
    DataFormat.SHAPEFILE: 10,
    DataFormat.FILE_GDB: 64,
}
DEFAULT_FIELD_NAME_MAX_LENGTH = 30
"""Limite prudente pour les géodatabases d'entreprise (Oracle : 30)."""

LOW_CAPACITY_WARNING = 500
"""En dessous, un avertissement signale que peu de chemins tiendront dans le champ."""

TESTED_FORMATS = frozenset({DataFormat.FILE_GDB})
"""Formats réellement validés. Les autres produisent un avertissement « non testé »."""


def classify_data_source(catalog_path: str, workspace_type: Optional[str] = None) -> DataFormat:
    """Déduit le format des données à partir du chemin (et du type d'espace de travail si connu)."""
    if (workspace_type or "").lower() == "remotedatabase":
        return DataFormat.ENTERPRISE_GDB
    path = (catalog_path or "").strip()
    lower = path.lower()
    if lower.startswith(("http://", "https://")):
        return DataFormat.FEATURE_SERVICE
    if lower.endswith(".shp"):
        return DataFormat.SHAPEFILE
    parts = [p.lower() for p in re.split(r"[\\/]", path)]
    if any(p.endswith(".sde") for p in parts):
        return DataFormat.ENTERPRISE_GDB
    if any(p.endswith(".gdb") for p in parts):
        return DataFormat.FILE_GDB
    return DataFormat.AUTRE


def guess_workspace(catalog_path: str, data_format: DataFormat) -> str:
    """Espace de travail contenant les données (``…\\base.gdb``, ``…\\connexion.sde``, dossier du shapefile)."""
    path = (catalog_path or "").strip()
    if data_format is DataFormat.SHAPEFILE:
        return ntpath.dirname(path)
    suffix = {DataFormat.FILE_GDB: ".gdb", DataFormat.ENTERPRISE_GDB: ".sde"}.get(data_format)
    if suffix:
        parts = re.split(r"([\\/])", path)
        for index in range(len(parts) - 1, -1, -1):
            if parts[index].lower().endswith(suffix):
                return "".join(parts[: index + 1])
    return ntpath.dirname(path)


# --- Sélection et requête de définition -------------------------------------


def parse_fid_set(fid_set: Optional[str]) -> List[int]:
    """Lit la propriété ``FIDSet`` d'une couche (``"1; 5; 8"``). Vide = aucune sélection."""
    if not fid_set:
        return []
    return [int(part) for part in re.split(r"[;,\s]+", fid_set.strip()) if part]


@dataclass
class LayerInfo:
    """Ce que l'outil doit savoir de la couche avant de travailler."""

    name: str
    catalog_path: str
    workspace: str
    data_format: DataFormat
    oid_field: str
    shape_field: Optional[str] = None
    is_layer: bool = True
    """Faux si l'entrée est un chemin de classe d'entités (pas de sélection possible)."""
    is_versioned: bool = False
    selection_count: Optional[int] = None
    """Nombre d'entités sélectionnées ; ``None`` s'il n'y a pas de sélection."""
    definition_query: Optional[str] = None
    scope_count: Optional[int] = None
    """Nombre d'entités réellement traitées (sélection et requête de définition appliquées)."""

    @property
    def has_selection(self) -> bool:
        return bool(self.selection_count)

    @property
    def needs_edit_session(self) -> bool:
        """Session de mise à jour (``arcpy.da.Editor``) requise : géodatabase d'entreprise ou versionnée."""
        return self.data_format is DataFormat.ENTERPRISE_GDB or self.is_versioned


def scope_messages(info: LayerInfo) -> List[Message]:
    """Explique à l'utilisateur quelles entités seront traitées (règle validée)."""
    messages: List[Message] = []
    if info.has_selection:
        messages.append(
            (
                "WARNING",
                "Sélection active sur la couche « {} » : seules les {} entité(s) sélectionnée(s) "
                "seront traitées. Effacez la sélection pour traiter toute la couche.".format(
                    info.name, info.selection_count
                ),
            )
        )
    else:
        count = "" if info.scope_count is None else " ({} entité(s))".format(info.scope_count)
        messages.append(
            ("INFO", "Aucune sélection : toutes les entités de la couche « {} »{} seront traitées.".format(info.name, count))
        )
    if info.definition_query:
        messages.append(
            (
                "WARNING",
                "Requête de définition active : « {} ». Les entités exclues par ce filtre ne sont "
                "ni lues ni modifiées.".format(info.definition_query),
            )
        )
    messages.append(("INFO", "Format des données : {} ({}).".format(info.data_format.value, info.catalog_path)))
    if info.data_format not in TESTED_FORMATS:
        messages.append(
            (
                "WARNING",
                "Format « {} » non encore validé pour GeoPDF Tools : vérifiez le résultat sur une copie "
                "des données.".format(info.data_format.value),
            )
        )
    if info.needs_edit_session:
        messages.append(("INFO", "Écriture dans une session de mise à jour (arcpy.da.Editor)."))
    return messages


# --- Champs -----------------------------------------------------------------


@dataclass
class FieldInfo:
    """Description d'un champ (copie de ``arcpy.Field``)."""

    name: str
    type: str
    length: Optional[int] = None
    editable: bool = True
    required: bool = False


TEXT_FIELD_TYPES = frozenset({"string", "text"})
IDENTIFIER_FIELD_TYPES = frozenset(
    {"string", "text", "integer", "smallinteger", "biginteger", "long", "short", "guid", "globalid", "oid"}
)
IDENTIFIER_FIELD_TYPES_WITH_WARNING = frozenset({"double", "single", "float"})


class FieldError(ValueError):
    """Problème bloquant sur un champ : rien n'est modifié."""


def check_identifier_field(field_info: Optional[FieldInfo], requested_name: str) -> List[Message]:
    """Vérifie que le champ identifiant existe et a un type exploitable."""
    if field_info is None:
        raise FieldError("Le champ identifiant « {} » n'existe pas dans la couche.".format(requested_name))
    kind = field_info.type.lower()
    if kind in IDENTIFIER_FIELD_TYPES:
        return []
    if kind in IDENTIFIER_FIELD_TYPES_WITH_WARNING:
        return [
            (
                "WARNING",
                "Le champ identifiant « {} » est de type réel ({}) : les valeurs entières (12.0) "
                "sont lues comme « 12 ».".format(field_info.name, field_info.type),
            )
        ]
    raise FieldError(
        "Le champ « {} » (type {}) ne peut pas servir d'identifiant.".format(field_info.name, field_info.type)
    )


@dataclass
class IdentifierRecords:
    """Identifiants lus dans la couche, prêts pour ``IdentifierIndex``."""

    records: List[Tuple[Hashable, Any]] = field(default_factory=list)
    ignored_count: int = 0
    """Valeurs NULL, vides ou composées uniquement d'espaces."""
    total: int = 0

    def messages(self, field_name: str) -> List[Message]:
        messages = [
            ("INFO", "{} entité(s) lue(s) dans le champ « {} ».".format(self.total, field_name)),
        ]
        if self.ignored_count:
            messages.append(
                (
                    "WARNING",
                    "{} entité(s) ignorée(s) : identifiant NULL, vide ou composé d'espaces.".format(self.ignored_count),
                )
            )
        return messages


def filter_identifier_rows(rows: Iterable[Sequence[Any]]) -> IdentifierRecords:
    """Garde les couples ``(OID, identifiant)`` exploitables ; compte les valeurs vides."""
    result = IdentifierRecords()
    for oid, value in rows:
        result.total += 1
        if normalize_identifier(value):
            result.records.append((oid, value))
        else:
            result.ignored_count += 1
    return result


class FieldAction(str, Enum):
    UTILISER = "UTILISER"
    CREER = "CREER"


@dataclass
class ResultFieldPlan:
    """Décision prise pour le champ résultat, avant toute modification."""

    name: str
    action: FieldAction
    capacity: FieldCapacity
    messages: List[Message] = field(default_factory=list)

    @property
    def length(self) -> Optional[int]:
        return self.capacity.max_length


_FIELD_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def plan_result_field(
    requested_name: str,
    existing: Optional[FieldInfo],
    create_if_missing: bool,
    data_format: DataFormat,
    protected_names: Iterable[str] = (),
) -> ResultFieldPlan:
    """Décide comment utiliser le champ résultat. Lève ``FieldError`` si c'est impossible.

    Ne modifie rien : la création effective est faite par ``ensure_result_field``.
    """
    name = (requested_name or "").strip()
    if not name:
        raise FieldError("Le nom du champ résultat est vide.")
    protected = {p.upper() for p in protected_names if p}
    if name.upper() in protected:
        raise FieldError(
            "Le champ « {} » est protégé (identifiant, ObjectID ou géométrie) : il ne peut pas recevoir "
            "les liens PDF.".format(name)
        )

    unit = LengthUnit.BYTES if data_format is DataFormat.SHAPEFILE else LengthUnit.CHARACTERS
    messages: List[Message] = []

    if existing is not None:
        if existing.type.lower() not in TEXT_FIELD_TYPES:
            raise FieldError(
                "Le champ « {} » existe mais n'est pas de type texte ({}). Il ne sera pas converti : "
                "choisissez un autre nom.".format(existing.name, existing.type)
            )
        if not existing.editable:
            raise FieldError("Le champ « {} » n'est pas modifiable.".format(existing.name))
        capacity = FieldCapacity(existing.length, unit)
        messages.append(
            ("INFO", "Champ résultat existant : « {} » (texte, capacité {}).".format(existing.name, capacity.describe()))
        )
        if existing.length is not None and existing.length < LOW_CAPACITY_WARNING:
            messages.append(
                (
                    "WARNING",
                    "Capacité du champ « {} » faible ({}) : les entités dont la liste de chemins dépasse "
                    "cette longueur ne seront pas mises à jour (aucune troncature).".format(
                        existing.name, capacity.describe()
                    ),
                )
            )
        return ResultFieldPlan(name=existing.name, action=FieldAction.UTILISER, capacity=capacity, messages=messages)

    if not create_if_missing:
        raise FieldError(
            "Le champ « {} » n'existe pas et sa création n'est pas autorisée. Cochez « Créer le champ "
            "s'il n'existe pas » ou choisissez un champ existant.".format(name)
        )
    if not _FIELD_NAME_PATTERN.match(name):
        raise FieldError(
            "Nom de champ invalide : « {} ». Utilisez des lettres, chiffres et « _ », en commençant par "
            "une lettre.".format(name)
        )
    max_name = FIELD_NAME_MAX_LENGTH.get(data_format, DEFAULT_FIELD_NAME_MAX_LENGTH)
    if len(name) > max_name:
        raise FieldError(
            "Nom de champ trop long pour ce format ({} > {} caractères) : ArcGIS le tronquerait.".format(
                len(name), max_name
            )
        )

    length = NEW_FIELD_LENGTH[data_format]
    capacity = FieldCapacity(length, unit)
    messages.append(("INFO", "Le champ « {} » sera créé (texte, {}).".format(name, capacity.describe())))
    if data_format is DataFormat.SHAPEFILE:
        messages.append(
            (
                "WARNING",
                "Shapefile : un champ texte est limité à 254 octets, soit en général 1 à 3 chemins. "
                "Les entités dont la liste dépasse cette limite ne seront pas mises à jour (aucune troncature). "
                "Une géodatabase fichier est recommandée.",
            )
        )
    return ResultFieldPlan(name=name, action=FieldAction.CREER, capacity=capacity, messages=messages)


# =============================================================================
# Partie 2 — fonctions ArcPy (à vérifier dans ArcGIS Pro)
# =============================================================================


def describe_layer(layer: Any) -> LayerInfo:
    """Décrit la couche : source, format, sélection, requête de définition, nombre d'entités."""
    arcpy = import_arcpy()
    desc = arcpy.Describe(layer)
    catalog_path = getattr(desc, "catalogPath", "") or str(layer)
    is_layer = getattr(desc, "dataType", "") in ("FeatureLayer", "Layer")

    data_format = classify_data_source(catalog_path)
    workspace = guess_workspace(catalog_path, data_format)
    try:
        workspace_type = getattr(arcpy.Describe(workspace), "workspaceType", None)
        data_format = classify_data_source(catalog_path, workspace_type)
    except Exception:  # espace de travail non descriptible (service, mémoire…)
        pass

    try:
        is_versioned = bool(getattr(arcpy.Describe(catalog_path), "isVersioned", False))
    except Exception:
        is_versioned = False

    selected = parse_fid_set(getattr(desc, "FIDSet", "")) if is_layer else []
    definition_query = (getattr(desc, "whereClause", "") or "").strip() if is_layer else ""

    return LayerInfo(
        name=getattr(desc, "name", str(layer)),
        catalog_path=catalog_path,
        workspace=workspace,
        data_format=data_format,
        oid_field=getattr(desc, "OIDFieldName", "OBJECTID"),
        shape_field=getattr(desc, "shapeFieldName", None),
        is_layer=is_layer,
        is_versioned=is_versioned,
        selection_count=len(selected) or None,
        definition_query=definition_query or None,
        scope_count=int(arcpy.management.GetCount(layer)[0]),
    )


def list_fields(layer: Any) -> Dict[str, FieldInfo]:
    """Champs de la couche, indexés par nom en majuscules."""
    arcpy = import_arcpy()
    return {
        f.name.upper(): FieldInfo(
            name=f.name,
            type=f.type,
            length=getattr(f, "length", None),
            editable=getattr(f, "editable", True),
            required=getattr(f, "required", False),
        )
        for f in arcpy.ListFields(layer)
    }


def read_identifier_records(layer: Any, id_field: str, search_cursor: Any = None) -> IdentifierRecords:
    """Lit une seule fois ``OID@`` et le champ identifiant (sélection et filtre respectés)."""
    if search_cursor is None:
        search_cursor = import_arcpy().da.SearchCursor
    with search_cursor(layer, ["OID@", id_field]) as cursor:
        return filter_identifier_rows(cursor)


def ensure_result_field(
    layer: Any,
    info: LayerInfo,
    field_name: str,
    create_if_missing: bool,
    id_field: str,
) -> ResultFieldPlan:
    """Vérifie le champ résultat et le crée si nécessaire et autorisé.

    ``AddField`` est le seul changement de schéma possible ; aucun autre champ
    n'est modifié, aucun champ n'est supprimé.
    """
    arcpy = import_arcpy()
    fields = list_fields(layer)
    plan = plan_result_field(
        field_name,
        fields.get(field_name.strip().upper()),
        create_if_missing,
        info.data_format,
        protected_names=[id_field, info.oid_field, info.shape_field or ""],
    )
    if plan.action is FieldAction.CREER:
        validated = arcpy.ValidateFieldName(plan.name, info.workspace)
        if validated != plan.name:
            raise FieldError(
                "Nom de champ refusé par ArcGIS : « {} » (suggestion : « {} »).".format(plan.name, validated)
            )
        arcpy.management.AddField(layer, plan.name, "TEXT", field_length=plan.length, field_alias="Liens PDF")
        created = list_fields(layer).get(plan.name.upper())
        if created is None:
            raise FieldError("Le champ « {} » n'a pas pu être créé.".format(plan.name))
        if created.length is not None and created.length != plan.length:
            # Capacité réelle retenue par ArcGIS : c'est elle qui protège contre la troncature.
            plan.capacity = FieldCapacity(created.length, plan.capacity.unit)
            plan.messages.append(
                ("WARNING", "Longueur réelle du champ créé : {}.".format(plan.capacity.describe()))
            )
    return plan


@contextmanager
def edit_session(info: LayerInfo) -> Iterator[bool]:
    """Session de mise à jour si nécessaire (géodatabase d'entreprise ou versionnée).

    En cas d'erreur dans le bloc, ``arcpy.da.Editor`` annule les modifications.
    Rend ``True`` si une session est active. Pour une géodatabase fichier ou
    un shapefile : aucune session (V1).
    """
    if not info.needs_edit_session:
        yield False
        return
    arcpy = import_arcpy()
    try:
        editor = arcpy.da.Editor(info.workspace, multiuser_mode=info.is_versioned)
    except TypeError:  # versions d'ArcGIS Pro sans le paramètre multiuser_mode
        editor = arcpy.da.Editor(info.workspace)
    with editor:
        yield True


def make_field_link_writer(layer: Any, info: LayerInfo, field_plan: ResultFieldPlan, mode: WriteMode) -> FieldLinkWriter:
    """``FieldLinkWriter`` branché sur les curseurs ArcPy de la couche."""
    return FieldLinkWriter(
        dataset=layer,
        field_name=field_plan.name,
        mode=mode,
        capacity=field_plan.capacity,
        edit_session=lambda: edit_session(info),
    )
