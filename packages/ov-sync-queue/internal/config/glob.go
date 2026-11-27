package config

import "github.com/bmatcuk/doublestar/v4"

func validGlob(g string) bool {
	return doublestar.ValidatePattern(g)
}
