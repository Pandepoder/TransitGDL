# Verificación del sistema experto sobre las rutas reales

Base de conocimiento versión 1.3, generado el 17/11/2026 en 33 s.

```bash
python scripts/verificacion_experto.py --salida docs/verificacion_experto.md
```

## 1. Diagnósticos sobre la red real

- **Rutas:** 247 (239 de camión, 5 de BRT, 3 de tren).
- **Escenarios:** 4 tipos de día × 6 horas (3:00, 8:00, 11:00, 14:00, 19:00, 23:00) × 3 climas = 17,784 diagnósticos, 5,589 de ellos en rutas sin salidas a esa hora.
- **Errores:** 0.
- **Mismo escenario, mismo diagnóstico:** 302 repetidos, 0 distintos.
- **Reglas que se activaron:** 40 de 64. Las demás necesitan datos que este recorrido no trae: incidencias, un conteo de flota o calificaciones de usuarios. Las cubren el barrido y los casos de aceptación.

| Contradicción o falta de trazabilidad | Diagnósticos | Ejemplo |
|---|---|---|
| Agregar y retirar unidades a la vez | 0 | — |
| Hora pico de la ciudad en fin de semana o festivo | 0 | — |
| Ruta sin servicio con acciones de operación | 0 | — |
| Ruta sin servicio con acciones de flota | 0 | — |
| «Pico no cubierto» con demanda baja | 0 | — |
| Acción sin cadena de reglas o con certeza fuera de rango | 0 | — |
| Agregar con menos unidades de las que pide el horario | 0 | — |
| Retirar con más unidades de las que pide el horario | 0 | — |

## 2. Pruebas metamórficas

Cada relación compara dos escenarios que solo difieren en una cosa y revisa que el cambio vaya en el sentido lógico, sin necesidad de conocer la respuesta exacta.

| Relación | Casos | Fallas | Ejemplo de falla |
|---|---|---|---|
| Un clima peor nunca baja la demanda | 4,065 | 0 | — |
| Un clima peor nunca mejora el servicio | 4,065 | 0 | — |
| La lluvia no degrada trenes ni BRT de vía exclusiva | 52 | 0 | — |
| Con lluvia o tormenta ninguna ruta retira unidades | 4,065 | 0 | — |
| Con el mismo horario, domingo o festivo nunca tiene más demanda que un día laboral | 3,666 | 0 | — |
| Una incidencia grave nunca mejora el servicio | 494 | 0 | — |
| Una incidencia grave siempre se avisa a los usuarios | 494 | 0 | — |

La relación de calendario deja fuera 2,316 pares en los que el domingo o el festivo ya traen un horario reducido y el día laboral no. El nivel de demanda se mide contra el servicio que el horario programa: si el horario ya bajó, la demanda puede quedar «normal» aunque en el día laboral, con más salidas, sea «baja».

## 3. Casos de aceptación

El resultado esperado se escribió antes de correr cada caso. Aprobados: 12 de 12.

| Caso | Esperado | Obtenido | Resultado |
|---|---|---|---|
| T15, laboral 8:00, tormenta | Demanda crítica; agregar unidades y alerta operativa | demanda critica, servicio degradado, flota agregar (65 → 98); acciones: emitir_alerta_operativa, agregar_unidades, reducir_velocidad_operativa, ampliar_ventana_eta | Aprobado |
| T15, laboral 8:00, lluvia | Demanda alta, no crítica; servicio degradado; ampliar la ventana de llegada | demanda alta, servicio degradado, flota agregar (57 → 72); acciones: agregar_unidades, ampliar_ventana_eta | Aprobado |
| T15, laboral 11:00, despejado | Hora valle: demanda normal y sin acciones de flota | demanda normal, servicio normal, flota sin cambio; acciones: ninguna | Aprobado |
| T15, festivo 8:00, despejado | Sin hora pico: demanda baja y no agregar unidades | demanda baja, servicio normal, flota reducir (40 → 34); acciones: reducir_unidades | Aprobado |
| T15, domingo 14:00, despejado | Sin «pico no cubierto»; no agregar unidades | demanda baja, servicio normal, flota reducir (40 → 34); acciones: reducir_unidades | Aprobado |
| Tren L1, laboral 8:00, lluvia | La lluvia no degrada el tren: servicio normal y sin agregar unidades | demanda normal, servicio normal, flota sin cambio; acciones: ninguna | Aprobado |
| Tren L1, laboral 3:00 | Sin servicio: solo informar que no opera | demanda normal, servicio normal, flota sin cambio; acciones: informar_sin_servicio | Aprobado |
| T15, laboral 11:00, niebla | Velocidad reducida por seguridad | demanda normal, servicio normal, flota sin cambio; acciones: reducir_velocidad_operativa | Aprobado |
| T15, laboral 11:00, incidencia crítica | Servicio crítico; avisar a los usuarios y sugerir una ruta alterna | demanda normal, servicio critico, flota sin cambio; acciones: notificar_usuarios, sugerir_ruta_alterna, ampliar_ventana_eta | Aprobado |
| T15, retraso crónico (6 calificaciones, 7.5 min tarde) | Ajustar el horario publicado y auditar la regularidad | demanda normal, servicio degradado, flota sin cambio; acciones: ajustar_horario_publicado, revisar_regularidad, ampliar_ventana_eta | Aprobado |
| T15, tormenta 8:00 con 120 unidades en servicio | Ya hay más de las que pide: mantener, sin agregar | decisión mantener; La flota actual (120) ya cubre lo recomendado (98); no hace falta agregar unidades. | Aprobado |
| ¿Por qué no retirar unidades? (T15, tormenta 8:00) | Explica que R62 necesita «reducir» y se concluyó «agregar» | R62 · Retirar unidades: no se cumple decision_flota == reducir (se concluyó decision_flota = agregar). | Aprobado |

## Lo que esta verificación no demuestra

Demuestra que el sistema está bien construido y que razona de forma coherente con su base de conocimiento sobre toda la red. No demuestra que sus conclusiones coincidan con lo que pasa en la calle: para eso hace falta comparar con una persona que conozca la operación (`docs/validacion/`) o con conteos reales de pasajeros.
