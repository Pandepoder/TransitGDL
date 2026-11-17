# Barrido del sistema experto

20,000 escenarios aleatorios (semilla 11) sobre la base de conocimiento versión 1.3, generados el 17/11/2026.

```bash
python scripts/barrido_experto.py --n 20000 --semilla 11
```

## Invariantes

Cada fila cuenta los diagnósticos que caen en una contradicción de sentido.

| Invariante | Base 1.1 (antes) | Base 1.3 |
|---|---|---|
| Agregar y retirar unidades a la vez | 0 | 0 |
| Hora pico de la ciudad en fin de semana o festivo | 2,618 | 0 |
| Ruta sin servicio con acciones distintas de informar o avisar | 67 | 0 |
| Ruta sin servicio con acciones de flota | 42 | 0 |
| Pico no cubierto con demanda baja | 593 | 0 |

## Cobertura de la base

Se usan 64 de 64 reglas.
Las menos usadas: R43 (346), R60 (369), R44 (425), R50 (441), R51 (441).

En promedio cada diagnóstico tarda 1.11 ms y dispara 13.2 reglas.

## Diagnósticos

| Nivel de demanda | Escenarios |
|---|---|
| baja | 28.7 % |
| normal | 34.5 % |
| alta | 34.5 % |
| critica | 2.3 % |

| Decisión de flota | Escenarios |
|---|---|
| sin cambio | 51.2 % |
| agregar | 41.2 % |
| reducir | 7.6 % |

| Acción recomendada | Escenarios |
|---|---|
| ampliar_ventana_eta | 60.5 % |
| agregar_unidades | 41.2 % |
| reducir_velocidad_operativa | 23.0 % |
| ajustar_horario_publicado | 22.7 % |
| sugerir_ruta_alterna | 22.0 % |
| notificar_usuarios | 20.2 % |
| revisar_regularidad | 18.3 % |
| activar_plan_contingencia | 13.4 % |
| reforzar_frecuencia | 13.1 % |
| reducir_unidades | 7.6 % |
| informar_sin_servicio | 4.9 % |
| emitir_alerta_operativa | 2.2 % |
| verificar_gps | 1.8 % |
