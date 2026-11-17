# Evaluación del ETA en vivo

Simulación de 2 h por escenario sobre las rutas de la zona CUCEI/Tonalá (semilla 7). Se cuentan las consultas en las que el método nuevo usa una unidad en vivo y se observó la llegada. Cuando la unidad con GPS más próxima tarda más de 1.5 frecuencias, el método nuevo usa la frecuencia programada (rastreo parcial) y esa consulta no entra en la comparación.

| Escenario | Método | Casos | Error medio | Error p90 | Sesgo | Llegada dentro del rango |
|---|---|---|---|---|---|---|
| Despejado | Anterior (línea recta) | 581 | 4.6 min | 11.0 min | +3.8 min | 61 % |
| Despejado | Nuevo (sobre la ruta) | 581 | 1.1 min | 2.1 min | +0.6 min | 80 % |
| Lluvia fuerte | Anterior (línea recta) | 606 | 5.9 min | 11.6 min | -0.9 min | 47 % |
| Lluvia fuerte | Nuevo (sobre la ruta) | 606 | 1.1 min | 2.4 min | +0.6 min | 82 % |

El simulador sigue el perfil de tiempos del horario con variación aleatoria de ritmo por unidad, así que favorece al método nuevo; la diferencia mide sobre todo los errores de geometría (línea recta) y de sentido del método anterior.
