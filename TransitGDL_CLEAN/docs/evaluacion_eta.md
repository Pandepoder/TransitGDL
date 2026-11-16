# Evaluación del ETA en vivo

Simulación de 2 h por escenario sobre las rutas de la zona CUCEI/Tonalá (semilla 7). Solo se cuentan las consultas con unidad a la vista para ambos métodos y con llegada observada.

| Escenario | Método | Casos | Error medio | Error p90 | Sesgo | Llegada dentro del rango |
|---|---|---|---|---|---|---|
| Despejado | Anterior (línea recta) | 1189 | 10.5 min | 27.4 min | +9.7 min | 35 % |
| Despejado | Nuevo (sobre la ruta) | 1189 | 1.8 min | 4.1 min | +1.0 min | 78 % |
| Lluvia fuerte | Anterior (línea recta) | 1176 | 12.2 min | 30.5 min | +7.9 min | 33 % |
| Lluvia fuerte | Nuevo (sobre la ruta) | 1176 | 2.2 min | 5.2 min | +1.3 min | 79 % |

El simulador sigue el perfil de tiempos del horario con variación aleatoria de ritmo por unidad, así que favorece al método nuevo; la diferencia mide sobre todo los errores de geometría (línea recta) y de sentido del método anterior.
