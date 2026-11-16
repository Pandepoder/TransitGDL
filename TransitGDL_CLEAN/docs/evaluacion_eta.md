# Evaluación del ETA en vivo

Simulación de 2 h por escenario sobre las rutas de la zona CUCEI/Tonalá (semilla 7). Solo se cuentan las consultas con unidad a la vista para ambos métodos y con llegada observada.

| Escenario | Método | Casos | Error medio | Error p90 | Sesgo | Llegada dentro del rango |
|---|---|---|---|---|---|---|
| Despejado | Anterior (línea recta) | 624 | 4.9 min | 11.6 min | +3.9 min | 58 % |
| Despejado | Nuevo (sobre la ruta) | 624 | 1.3 min | 2.2 min | +0.8 min | 81 % |
| Lluvia fuerte | Anterior (línea recta) | 580 | 5.8 min | 10.9 min | -0.6 min | 47 % |
| Lluvia fuerte | Nuevo (sobre la ruta) | 580 | 1.4 min | 2.5 min | +0.8 min | 83 % |

El simulador sigue el perfil de tiempos del horario con variación aleatoria de ritmo por unidad, así que favorece al método nuevo; la diferencia mide sobre todo los errores de geometría (línea recta) y de sentido del método anterior.
