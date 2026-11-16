# Evaluación del ETA en vivo

Simulación de 2 h por escenario sobre las rutas de la zona CUCEI/Tonalá (semilla 7). Se cuentan las consultas en las que el método nuevo usa una unidad en vivo y se observó la llegada. Cuando la unidad con GPS más próxima tarda más de 1.5 frecuencias, el método nuevo usa la frecuencia programada (rastreo parcial) y esa consulta no entra en la comparación.

| Escenario | Método | Casos | Error medio | Error p90 | Sesgo | Llegada dentro del rango |
|---|---|---|---|---|---|---|
| Despejado | Anterior (línea recta) | 589 | 3.9 min | 10.0 min | +3.2 min | 63 % |
| Despejado | Nuevo (sobre la ruta) | 589 | 0.9 min | 1.7 min | +0.4 min | 87 % |
| Lluvia fuerte | Anterior (línea recta) | 631 | 5.9 min | 11.2 min | -0.7 min | 47 % |
| Lluvia fuerte | Nuevo (sobre la ruta) | 631 | 1.1 min | 2.3 min | +0.6 min | 78 % |

El simulador sigue el perfil de tiempos del horario con variación aleatoria de ritmo por unidad, así que favorece al método nuevo; la diferencia mide sobre todo los errores de geometría (línea recta) y de sentido del método anterior.
