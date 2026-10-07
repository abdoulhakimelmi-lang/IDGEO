"""Table d'enregistrement pour tester ``FieldLinkWriter`` sans ArcGIS Pro.

Ce n'est PAS une simulation d'ArcGIS : elle ne reproduit ni les verrous, ni
les sessions de mise à jour, ni les sélections, ni les types de champs. Elle
sert uniquement à vérifier **ce que notre code demande** aux curseurs :

- quels champs sont demandés (``requests``) ;
- quelles lignes sont modifiées, et avec quelles valeurs (``updates``) ;
- qu'aucune suppression ni insertion n'est possible (les curseurs n'exposent
  pas ``deleteRow`` / ``insertRow`` : tout appel lèverait ``AttributeError``).
"""

import copy


class RecordingTable:
    def __init__(self, rows, oid_field="OBJECTID"):
        """``rows`` : liste de dictionnaires {nom de champ: valeur}, avec l'OID et une géométrie."""
        self.oid_field = oid_field
        self.rows = [dict(row) for row in rows]
        self.initial = copy.deepcopy(self.rows)
        self.requests = []  # (type de curseur, liste de champs)
        self.updates = []  # (oid, liste de valeurs écrites)
        self.fail_after = None  # lever une erreur après N écritures (test d'échec)

    def _value(self, row, name):
        if name == "OID@":
            return row[self.oid_field]
        if name not in row:
            raise RuntimeError("Champ inconnu : {}".format(name))
        return row[name]

    def search_cursor(self, dataset, fields):
        self.requests.append(("search", list(fields)))
        return _SearchCursor(self, list(fields))

    def update_cursor(self, dataset, fields):
        self.requests.append(("update", list(fields)))
        return _UpdateCursor(self, list(fields))

    def value(self, oid, name):
        return next(r[name] for r in self.rows if r[self.oid_field] == oid)


class _SearchCursor:
    def __init__(self, table, fields):
        self.table = table
        self.fields = fields

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for row in self.table.rows:
            yield [self.table._value(row, f) for f in self.fields]


class _UpdateCursor(_SearchCursor):
    def __iter__(self):
        for row in self.table.rows:
            self._current = row
            yield [self.table._value(row, f) for f in self.fields]

    def updateRow(self, values):
        table = self.table
        if table.fail_after is not None and len(table.updates) >= table.fail_after:
            raise RuntimeError("Erreur simulée pendant l'écriture")
        if len(values) != len(self.fields):
            raise RuntimeError("Nombre de valeurs incorrect")
        for name, value in zip(self.fields, values):
            if name == "OID@":
                if value != self._current[table.oid_field]:
                    raise RuntimeError("Tentative de modification de l'OID")
                continue
            self._current[name] = value
        table.updates.append((self._current[table.oid_field], list(values)))
