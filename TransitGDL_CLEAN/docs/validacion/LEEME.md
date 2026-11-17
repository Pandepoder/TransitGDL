# Validación con experto

El barrido (`docs/barrido_experto.md`) prueba que el sistema experto es coherente. Esta validación prueba otra cosa: que sus diagnósticos se parecen a los de una persona que conoce la operación.

## Qué hace falta

- **Una persona** con experiencia en la operación del transporte del AMG: chofer, checador, despachador o personal de la SETRAN.
- **15 minutos** para llenar `formulario_experto.html`, impreso o en pantalla. Son 12 situaciones sobre rutas reales, y en cada una responde tres preguntas: la demanda, qué haría con la flota y su acción principal.
- **No hay que explicarle qué respondió el sistema.** El formulario no lo muestra, para no influir en sus respuestas. Las respuestas del sistema están en `escenarios.json`.

## Pasos

1. **Genera los escenarios** con la versión actual de las reglas. Así se crean `escenarios.json`, `formulario_experto.html` y `respuestas_plantilla.csv`:

   ```bash
   python scripts/validacion_experto.py generar
   ```

2. **Aplica el formulario** e indica el puesto y los años de experiencia de la persona.
3. **Captura sus respuestas** en una copia de `respuestas_plantilla.csv`, una fila por escenario:
   - `nivel_demanda`: baja, normal, alta o crítica.
   - `decision_flota`: agregar, mantener o retirar.
   - `accion`: el texto o el identificador de la acción, o «ninguna».
   - `comentario`: lo que haya dicho.

   Deja vacía la fila de una situación que no contestó.
4. **Calcula el acuerdo:**

   ```bash
   python scripts/validacion_experto.py evaluar docs/validacion/respuestas.csv
   ```

   El resultado queda en `resultado.md`:
   - el porcentaje de acuerdo por pregunta;
   - el kappa de Cohen, que descuenta el acuerdo por azar;
   - la matriz de confusión del nivel de demanda;
   - cada desacuerdo con el comentario de la persona.

## Cómo leer el resultado

| Kappa | Acuerdo (Landis y Koch, 1977) |
|---|---|
| 0.81 a 1.00 | Casi perfecto |
| 0.61 a 0.80 | Sustancial |
| 0.41 a 0.60 | Moderado |
| 0.21 a 0.40 | Aceptable |
| 0.00 a 0.20 | Leve |

**Los desacuerdos son lo más útil.** Si la persona explica por qué decidiría otra cosa, ese criterio se puede escribir como regla nueva o como ajuste de una existente en `core/expert/rules.yaml`. Eso es adquisición de conocimiento.
